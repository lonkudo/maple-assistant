"""本地测谎 service: start it with the app, stop it with the app.

本地测谎 talks to the CT partner API on ``127.0.0.1:8765``.  The operator's rule:
"when the application starts, if this service is not started, start the service;
when the application stops [stop it], and show a hint on 图层校准与巡逻".

Only a service THIS app started is ever stopped: an instance the operator
launched by hand (双击 启动_后台.bat) must survive the assistant.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger(__name__)

# The packaged service folder, relative to the install root.  ``release_now.ps1``
# does not ship it, so a missing folder is a normal state, not an error.
SERVICE_FOLDER = Path("CT_Lie3_KR_partner_api_DML") / "CT_Lie3_KR_partner_api_DML"
LAUNCHER_NAMES = ("启动_后台.vbs", "launch_background.vbs")
# The vendor renames the service binary on almost every build: TaskBroker0a5a.exe
# in the 1.2.x package, AppHostd83e.exe in the first 2026-11 drop, and
# HostSvc3da4.exe in the one the operator installed now.  Discovery therefore
# never trusts one name: it asks the package's own data/build_profile.json
# (client_exe_stem) first and falls back to any *.exe beside the launchers.
BINARY_PATTERNS = (
    "TaskBroker*.exe", "AppHost*.exe", "HostSvc*.exe", "*Svc*.exe", "*.exe",
)
# Readiness: licence validation + WS handshake happen before 8765 listens, which
# takes ~20s cold, so the startup wait is generous but never blocks the UI.
# First launch after an update can unpack/scan the 339 MB service, validate the
# card and finish its WS handshake before 8765 exists.  This runs only in the
# background supervisor; 90 s keeps that genuine startup as 启动中 rather than
# reporting a false failure.
START_WAIT_SECONDS = 90.0
PROBE_INTERVAL_SECONDS = 0.5
# The vendor's own startup gate: it validates the monthly card, then locks its
# built-in "221+117" WS candidate list, randomly probes two of those ports and
# ABORTS the start when both handshakes fail ("坐标云启动握手失败，同行服务未启动").
# That list is compiled into the vendor binary (the trimmed build_profile.json does
# NOT change it - its log still says "锁定 221+117 共 2 WS 口"), so one candidate,
# 221.10.121.69:44444, is a retired deployment that always times out, and the whole
# start depends on 117.50.223.113:8888 finishing the WS handshake.  While that
# deployment does not answer, EVERY attempt fails the same way, which is what the
# 2026-11-15 field log shows (8 attempts in 2.5 minutes, all "坐标云启动握手失败").
# Three consequences are handled here:
#   * a start attempt whose vendor process has already exited is reported as a
#     failure at once (not after the full 90 s budget), quoting the vendor's own
#     reason from data\logs\client.log in the LOG;
#   * a launch that is still BOOTING is never counted as a failed attempt;
#   * after the budget the panel says 本地测谎：启动失败 (no reason: the reason is a
#     developer sentence and stays in the log) and the supervisor keeps watching
#     at a slow cadence, so a deployment that recovers is picked up without
#     toggling the radio.
VENDOR_GONE_GRACE_SECONDS = 6.0
MAX_AUTO_START_ATTEMPTS = 8
# Retry gap after a start attempt that FAILED (the vendor aborted or its process
# exited).  The probe the vendor runs costs ~2-4 s, so a short gap turns the coin
# flip into a success within ~10-20 s instead of the 10 s supervisor cadence plus
# a 90 s budget per attempt.  A healthy-but-slow boot still uses
# ``SUPERVISE_INTERVAL_SECONDS`` because it never reaches this path.
START_RETRY_FAST_SECONDS = 3.0
# Cadence once the attempt budget is spent: the supervisor keeps probing (and
# retrying) instead of quitting, because a deployment-side gate failure is a
# temporary state of the SERVER, not a reason to require a radio toggle.
SLOW_RETRY_SECONDS = 60.0
# Pre-start supervision: how often the supervisor re-checks that 8765 answers.
SUPERVISE_INTERVAL_SECONDS = 10.0
# Startup supervision: a first launch can exit while the licence/WS are being
# provisioned, so a service that is not listening is retried after this delay.
RETRY_BACKOFF_SECONDS = 20.0
# 本地测谎 must not start on information older than the current session: the card
# in the vendor config may be a leftover.  The operator's rule (2026-11-13): wait
# for a complete licence heartbeat, take the card from the reply, then start.  A
# validated reply arms the service immediately; if none arrives within this
# window the card already in the service config is used anyway (an offline start
# must still work) and the log says so.
CARD_REPLY_WAIT_SECONDS = 30.0
# A service process from the previous app run can still hold 8765 for a moment
# while it shuts down.  The start waits this long for it to answer or exit instead
# of launching a second instance that the vendor binary would refuse.
STALE_SERVICE_GRACE_SECONDS = 8.0
# Ownership record for the vendor service this install launched (see
# _remember_ownership).  Kept under work\ so the log cleaner leaves it alone.
OWNER_MARKER_NAME = "local-lie-owner.json"
# How long a stop waits for the vendor process to really exit.  8765 must be free
# before the next start, otherwise the new vendor is refused as a second instance.
SERVICE_STOP_WAIT_SECONDS = 6.0

# Re-entrant on purpose: several helpers take it (``has_online_card`` and
# ``card_known`` read the state) and the failure paths of ``ensure_started`` call
# them from inside their own ``with _lock`` block.  With a plain Lock that nesting
# deadlocked the calling thread **while holding the lock**, which froze every later
# probe, hint and supervisor iteration - the field symptom "本地测谎 一直显示正在
# 启动/没有任何反应" after one start that could not come up (no CT card, licence
# refused, or a slow cold start).
_lock = threading.RLock()

# A reader on the Tk thread must never WAIT for a writer that is inside a long
# operation.  ``_lock`` used to be held across the start sequence, and ``_probe``
# (an HTTP health check with a 3 s timeout) ran inside it - so the poll's
# ``hint_state()`` could block the whole UI: the window stopped pumping messages,
# Windows declared it "not responding" while the header still read
# 正在验证在线授权, and the next launch was refused as a duplicate (field report:
# the restarted instance froze there, the second start flashed closed, the third
# worked).  Reads therefore try the lock briefly and fall back to the last
# published copy, which is at most one start-attempt old.
STATE_READ_LOCK_TIMEOUT_SECONDS = 0.01
_last_state_snapshot: dict[str, object] = {}


def read_state() -> dict[str, object]:
    """A copy of the service state that never blocks the calling thread."""

    global _last_state_snapshot
    if _lock.acquire(timeout=STATE_READ_LOCK_TIMEOUT_SECONDS):
        try:
            snapshot = dict(_state)
            _last_state_snapshot = snapshot
            return snapshot
        finally:
            _lock.release()
    return dict(_last_state_snapshot)


_state: dict[str, object] = {
    "state": "idle",       # idle | missing | starting | running | failed | external
    "note": "",
    "root": "",
    "started_by_app": False,
    "pids": [],
    # Set by note_card_absent(): the licence reply said this code has no CT card, so
    # a cached card in the service config must not keep 本地测谎 selectable.
    "card_denied": False,
}


# Seed the lock-free fallback so the first reader during a busy writer sees the
# initial state instead of an empty dict.
_last_state_snapshot = dict(_state)


class LocalLieServiceError(RuntimeError):
    """The local service cannot be used or activated; the text is panel-ready."""


def find_service_root(app_root: Path | str) -> Optional[Path]:
    """The folder holding the packaged CT partner service, or None.

    The install root is NEVER a service folder.  ``BINARY_PATTERNS`` ends with
    ``*.exe``, so the old third candidate made the app's own folder match as soon
    as the vendor package was missing (``release_now.ps1`` does not ship it): the
    "service binaries" then were ``MoonEclipse.exe`` itself, and a start attempt
    launched a second copy of the assistant - the app vanished without a
    traceback, which reads exactly like a flash close.  A candidate now has to hold
    a vendor artefact: the package's own launcher, or an executable that is not
    this app.
    """

    root = Path(app_root)
    candidates = [
        root / SERVICE_FOLDER,
        root / SERVICE_FOLDER.parent,
    ]
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
        except OSError:
            continue
        if any((candidate / name).is_file() for name in LAUNCHER_NAMES):
            return candidate
        if _binary_names(candidate):
            return candidate
    return None


def _probe() -> tuple[bool, str]:
    try:
        from local_lie_backend import probe_local_lie_service

        return probe_local_lie_service()
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return False, f"本地测谎探测失败：{exc}"


def is_running() -> bool:
    return _probe()[0]


def _vendor_log_reason(root: Path) -> str:
    """The vendor service's own last failure line, made panel-ready, or "".

    The vendor aborts a start for reasons only its log knows (a rejected monthly
    card, a coordinate-cloud handshake that failed on both probed ports).  Reading
    that line is what lets the panel say WHY instead of a bare timeout - the 16:02
    field log failed with "坐标云启动握手失败" while our side could only see "8765
    did not answer".
    """

    path = Path(root) / "data" / "logs" / "client.log"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536), os.SEEK_SET)
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    reason = ""
    # The SUMMARY line (the vendor's own [ERROR] "坐标云启动握手失败 … 原因：
    # RoiTrackSvc 全部 2 个端点握手失败: timed out") hides the only actionable
    # detail: the per-endpoint [WARNING] from roitrack.ws_client says WHY, e.g.
    # ``handshake 失败: {'type': 'auth_denied', 'message': 'quota_exceeded', …}``.
    # Prefer that line when the tail has one - it is the newest specific failure.
    for line in tail.splitlines():
        if "handshake 失败" in line or "auth_denied" in line:
            reason = line
    if not reason:
        for line in tail.splitlines():
            if "[ERROR]" in line or "[CRITICAL]" in line:
                reason = line
    if not reason:
        for line in tail.splitlines():
            if "[WARNING]" in line:
                reason = line
    if not reason:
        return ""
    # Drop "[timestamp] [LEVEL] logger:" and keep the message.
    for marker in ("[ERROR] ", "[CRITICAL] ", "[WARNING] "):
        index = reason.find(marker)
        if index >= 0:
            reason = reason[index + len(marker):]
            break
    if ":" in reason:
        head, _sep, rest = reason.partition(":")
        if head and " " not in head and len(head) < 48:
            reason = rest.strip()
    reason = " ".join(str(reason).split())
    # The vendor appends its whole diagnosis on one line, separated by " | "
    # ("原因：… | | 开发调试可设 CT_PARTNER_WS_STARTUP_OPTIONAL=1 …").  Everything
    # after the first separator is advice for an integrator, not this machine's
    # state, and quoting it made every supervisor line 300 characters long.
    reason = reason.split(" |")[0].strip()
    return reason[:120]


def _rotate_rejected_product_key(app_root: Path | str, reason: str) -> bool:
    """Swap the local WS key after the tracking server REJECTED the current one.

    ``handshake 失败: {'type': 'auth_denied', 'message':
    'unknown_or_deleted_product_key'}`` is an auth verdict about the KEY itself,
    not a timeout and not a stale instance: the key in
    ``data/ct_login_config.json`` is unknown to the tracking server (a revoked
    packaged constant, or a partner key that was deleted/re-issued).  Retrying it
    can never succeed, and killing and restarting the vendor service only repeats
    the same denial - so the config is rotated to the one alternative this
    machine has: the package's own key, or the key the licence heartbeat
    delivered, whichever is not the rejected one.

    Rotated at most ONCE per app session, so a machine where both keys are
    rejected cannot ping-pong between them: after that the failure is reported
    with its real reason.
    """

    global _product_key_rotated
    text = str(reason or "").casefold()
    if ("unknown_or_deleted_product_key" not in text
            and "invalid_product_key" not in text):
        return False
    if _product_key_rotated:
        return False
    path = service_credentials_path(app_root)
    if path is None or not path.is_file():
        return False
    settings = _load_service_settings(path, what="the service credentials")
    if settings is None:
        return False
    current = normalise_credential(settings.get("lie_product_key"))
    candidates: list[str] = []
    # The heartbeat key FIRST: it is the credential the tracking server was just
    # measured to accept (auth=True, quota_left=100), while the constant this
    # package ships is answered with ``unknown_or_deleted_product_key``.
    try:
        from auto_lie_secret import get_server_secret

        heartbeat_key = str(get_server_secret() or "").strip()
    except Exception:  # noqa: BLE001 - the heartbeat key is optional here
        heartbeat_key = ""
    if heartbeat_key:
        candidates.append(heartbeat_key)
    package_key = _build_profile_ws_key(app_root)
    if package_key:
        candidates.append(package_key)
    replacement = next(
        (candidate for candidate in candidates
         if candidate and candidate.casefold() != current.casefold()),
        "",
    )
    if not replacement:
        LOG.warning(
            "local lie service: the tracking server rejected the WS product key "
            "(%s) and this machine has no different key to try; the card/keys must "
            "be re-issued by the partner server", _mask(current) or "none",
        )
        return False
    settings["lie_product_key"] = replacement
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the rejected WS key could not be replaced (%s)", exc)
        return False
    _product_key_rotated = True
    LOG.warning(
        "local lie service: the tracking server rejected the WS product key (%s: "
        "unknown_or_deleted_product_key); switched the local service to %s and "
        "will retry the handshake",
        _mask(current) or "none", _mask(replacement),
    )
    return True


def _short_reason(text: str, limit: int = 24) -> str:
    """The first useful clause of a failure text, short enough for the panel.

    ``本地测谎服务启动失败（服务进程已退出：坐标云启动握手失败，同行服务未启动（与
    GUI 首次自动握手一致…`` reduces to ``坐标云启动握手失败`` - the one fact the
    operator needs on the hint line.

    The vendor's own messages are mapped to a short label FIRST, because its
    handshake denial carries the only actionable fact in an English dict:
    ``handshake 失败: {'type': 'auth_denied', 'message': 'quota_exceeded', …}``
    must read 坐标云额度不足, not a truncated dict.
    """

    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    known = (
        ("quota_exceeded", "坐标云额度不足"),
        ("auth_denied", "坐标云拒绝授权"),
        ("no_valid_license", "月卡验票失败"),
        ("activate_failed", "月卡验票失败"),
        ("license_superseded", "月卡被其他终端占用"),
        ("winerror 10053", "坐标云连接被重置"),
        ("winerror 10054", "坐标云连接被重置"),
        ("强迫关闭", "坐标云连接被重置"),
        ("连接超时", "坐标云端点超时"),
        ("timed out", "坐标云端点超时"),
        ("connection timed out", "坐标云端点超时"),
        ("坐标云启动握手失败", "坐标云启动握手失败"),
    )
    for needle, label in known:
        if needle in lowered:
            return label
    for marker in ("原因：", "原因:", "退出：", "退出:", "失败：", "失败:"):
        index = cleaned.find(marker)
        if index >= 0:
            cleaned = cleaned[index + len(marker):]
            break
    else:
        for opening, closing in (("（", "）"), ("(", ")")):
            start = cleaned.find(opening)
            end = cleaned.find(closing, start + 1) if start >= 0 else -1
            if 0 <= start < end:
                inside = cleaned[start + 1:end].strip(" ：:")
                if inside:
                    cleaned = inside
                    break
    for stop in (" |", "；", ";", "，", ",", "。", "（", "(", "）", ")"):
        index = cleaned.find(stop)
        if 0 < index:
            cleaned = cleaned[:index]
            break
    return cleaned.strip(" ：:。…")[:limit]


def failure_reason(limit: int = 24) -> str:
    """Short, panel-ready reason for the last failed start ("" when none)."""

    with _lock:
        if str(_state.get("state") or "") != "failed":
            return ""
        note = str(_state.get("note") or "")
    return _short_reason(note, limit)


def _log_vendor_reason(root: Path, prefix: str) -> str:
    """Log the vendor's reason once and return it ("" when the log is silent)."""

    reason = _vendor_log_reason(root)
    if reason:
        LOG.warning("%s: %s", prefix, reason)
    return reason


def _pid_is_service_binary(pid: int, image: str, root: Path, own: set[str],
                           me: int) -> bool:
    """Whether ``pid``/``image`` is a vendor process of ``root`` - never us.

    Path verification is the whole point of this check: an image NAME is not
    evidence (``python.exe``/``MoonEclipse.exe``/a same-named copy in another
    folder all match one), and killing by name is how this feature could vanish
    its own host process.
    """

    if int(pid) in (0, 4) or int(pid) == me:
        return False
    folded = str(image or "").casefold()
    if not folded.endswith(".exe"):
        return False
    if not folded.startswith(str(root).casefold()):
        return False
    if Path(folded).name in own:
        return False
    return True


def _service_pids(root: Path) -> list[int]:
    """PIDs whose executable really lives inside ``root`` (path verified).

    This program is never one of them (see ``_own_image_names``): a pid list that
    contained the assistant made ``stop_started_service`` kill it, and the operator
    saw a flash close the moment 本地测谎 was started.  When no privileged query is
    available the sweep falls back to ``psutil`` (a product dependency) and still
    verifies every executable path - it never resolves a pid by image name alone.
    """

    pids: list[int] = []
    own = _own_image_names()
    me = os.getpid()
    try:
        wanted = Path(root).resolve()
    except OSError:
        wanted = Path(root)
    try:
        import win32api  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]

        for pid in win32process.EnumProcesses():
            if int(pid) in (0, 4) or int(pid) == me:
                continue
            handle = None
            try:
                handle = win32api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
                path = str(win32process.GetModuleFileNameEx(handle, 0) or "")
            except Exception:  # noqa: BLE001 - other users' processes deny query
                continue
            finally:
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:  # noqa: BLE001
                        pass
            if _pid_is_service_binary(int(pid), path, wanted, own, me):
                pids.append(int(pid))
    except Exception:  # noqa: BLE001 - pywin32 missing: use psutil paths
        LOG.debug("local lie service: pywin32 pid query unavailable", exc_info=True)
    if not pids:
        try:
            import psutil  # type: ignore[import-not-found]

            for process in psutil.process_iter(["pid", "name"]):
                try:
                    image = str(process.exe() or "")
                except Exception:  # noqa: BLE001 - a dead/denied pid has no path
                    continue
                pid = int(process.info.get("pid") or 0)
                if _pid_is_service_binary(pid, image, wanted, own, me):
                    pids.append(pid)
        except Exception:  # noqa: BLE001 - no psutil either: report nothing
            LOG.debug("local lie service: psutil pid query unavailable", exc_info=True)
    return sorted(set(pids))


def _build_profile_exe_stem(root: Path) -> str:
    """The service binary name the package itself declares, or ""."""

    profile = _read_service_settings(Path(root) / "data" / "build_profile.json")
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("client_exe_stem") or "").strip()


def _own_image_names() -> set[str]:
    """Image names that are THIS program, never the vendor service.

    ``BINARY_PATTERNS`` ends with a bare ``*.exe``, so any folder sweep can pick
    up the assistant itself (``MoonEclipse.exe`` in a frozen deployment, the
    interpreter in a source one).  Treating it as a "service binary" is what let a
    start attempt launch a second copy of the app, and what let a stop send a
    SIGTERM to the app's own pid - both look like a flash close with no trace.
    """

    names = {"mooneclipse.exe", "todohelper.exe", "mapleassistant.exe",
             "python.exe", "pythonw.exe"}
    try:
        names.add(Path(sys.executable).name.casefold())
    except Exception:  # noqa: BLE001 - a missing sys.executable changes nothing
        pass
    return names


def _binary_names(root: Path) -> list[str]:
    """Executables in the service folder, the package's declared one first.

    Never this program's own image (see ``_own_image_names``).
    """

    own = _own_image_names()
    names: list[str] = []
    stem = _build_profile_exe_stem(root)
    if stem and (root / f"{stem}.exe").is_file():
        names.append(f"{stem}.exe")
    for pattern in BINARY_PATTERNS:
        try:
            names.extend(item.name for item in root.glob(pattern))
        except OSError:
            continue
    ordered: list[str] = []
    for name in names:
        if name.casefold() in own:
            continue
        if name not in ordered:
            ordered.append(name)
    return ordered


def _pids_by_name(image: str) -> list[int]:
    """PIDs of every process with this IMAGE NAME (no path check).

    Diagnostics only: a name is not proof of provenance, so nothing in this module
    kills a process found this way (see ``_service_pids`` for the path-verified
    variant used by starts and stops).
    """

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in (completed.stdout or "").splitlines():
        parts = [item.strip('"') for item in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def scrub_service_credentials(app_root: Path | str) -> bool:
    """Remove the plaintext CT card and WS key once the service has read them.

    The vendor reads ``data/ct_login_config.json`` (card + product key) at
    startup, so the two credentials only have to exist for the few seconds
    between the write and the moment 8765 answers.  Keeping them afterwards left
    a stale key on disk that made the NEXT start fail (the field state
    ``"partition_license_code": "CT_IN-…", "lie_product_key": "LIE-PLANET-001"``)
    and it kept live credentials in plain text where anyone could read them.

    So: every launch rewrites both fields from the in-memory credentials the
    licence heartbeat delivered, and every outcome blanks them again.  Everything
    else in the document (authorization URL, backend switches, VM flags, the
    ``lie3_*`` tuning) is left exactly as the package shipped it.

    Returns True when the file changed.
    """

    root = find_service_root(app_root)
    if root is None:
        return False
    path = service_credentials_path(app_root)
    if path is None or not path.is_file():
        return False
    settings = _read_service_settings(path)
    if not isinstance(settings, dict):
        return False
    card = normalise_credential(settings.get("partner_license_code"))
    key = normalise_credential(settings.get("lie_product_key"))
    if not card and not key:
        return False                  # already clean
    settings["partner_license_code"] = ""
    settings["lie_product_key"] = ""
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: credentials could not be scrubbed (%s)", exc)
        return False
    LOG.info(
        "local lie service: removed the plaintext credentials from %s after the "
        "service read them (card=%s, key=%s); the next launch rewrites them from "
        "memory", path.name, _mask(card) or "none", _mask(key) or "none",
    )
    return True


def rewrite_service_credentials_from_memory(app_root: Path | str) -> bool:
    """Write the card and the WS key from this session's memory, just before a launch.

    The config is scrubbed again as soon as the service has read it, so every
    launch starts from two empty fields: this is the one place that puts the live
    values back - the CT card the licence reply delivered plus the WS key the
    heartbeat delivered - and nothing is ever taken from what a previous run left
    on disk.

    Returns True when the file changed.
    """

    with _lock:
        card = normalise_credential(_state.get("online_card"))
        denied = bool(_state.get("card_denied"))
    try:
        from auto_lie_secret import get_server_secret

        key = normalise_credential(get_server_secret())
    except Exception:  # noqa: BLE001 - the key is optional
        key = ""
    if denied:
        card = ""
    path = service_credentials_path(app_root)
    settings = _read_service_settings(path) if path is not None else None
    if not isinstance(settings, dict):
        return False
    stored_card = normalise_credential(settings.get("partner_license_code"))
    stored_key = normalise_credential(settings.get("lie_product_key"))
    need_card = bool(card) and stored_card != card
    need_key = bool(key) and stored_key != key
    # MEMORY IS ALWAYS THE NEWEST: a value that exists only on disk is stale by
    # definition (an earlier run's card/constant/key) and must never be used, so
    # it is removed rather than kept.
    stale_card = bool(stored_card) and not card
    stale_key = bool(stored_key) and not key
    if not (need_card or need_key or stale_card or stale_key):
        return False
    if stale_card or stale_key:
        settings["partner_license_code"] = card
        settings["lie_product_key"] = key
        try:
            _write_service_settings(path, settings)
        except OSError as exc:
            LOG.warning("local lie service: stale credentials could not be removed (%s)", exc)
            return False
        LOG.info(
            "local lie service: removed stale on-disk credentials that this session "
            "does not hold (card=%s, key=%s); memory is the authority",
            _mask(stored_card) or "none", _mask(stored_key) or "none",
        )
        return True
    if need_card:
        # write_credentials also pins the auth URL and syncs the vendor's own
        # remembered code; it writes the key too when the field is writable.
        try:
            write_credentials(app_root, card, key if need_key else "")
        except LocalLieServiceError as exc:
            LOG.warning("local lie service: the CT card could not be restored (%s)", exc)
            if not need_key:
                return False
        return True
    # Only the key is missing (the card is already in place).
    settings["lie_product_key"] = key
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the WS key could not be restored (%s)", exc)
        return False
    LOG.info(
        "local lie service: rewrote the WS key from memory before the start (%s)",
        _mask(key),
    )
    return True


def _credentials_rewritable_from_memory() -> bool:
    """Whether this session holds credentials it can rewrite before a launch."""

    try:
        with _lock:
            card = str(_state.get("online_card") or "").strip()
    except Exception:  # noqa: BLE001
        card = ""
    try:
        from auto_lie_secret import get_server_secret

        key = str(get_server_secret() or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    return bool(card or key)


def _launch(root: Path) -> Optional[subprocess.Popen]:
    """Start the packaged service hidden (WSH window style 0).

    The binaries come from ``_binary_names``, which already excludes this program's
    own image: a folder sweep must never be able to start a second copy of the
    assistant in place of the vendor service (that copy fought the first one for the
    single-instance mutex and the operator saw the window disappear).
    """

    for name in LAUNCHER_NAMES:
        launcher = root / name
        if not launcher.is_file():
            continue
        try:
            return subprocess.Popen(
                ["wscript.exe", "//nologo", str(launcher)],
                cwd=str(root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            LOG.warning("local lie service: %s could not start: %s", name, exc)
    # No launcher: start the binary directly, hidden.
    for binary in _binary_names(root):
        try:
            return subprocess.Popen(
                [str(root / binary)], cwd=str(root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            LOG.warning("local lie service: %s could not start: %s", binary, exc)
    return None


# The launcher of the LAST attempt, kept so a slow first start can be watched
# instead of guessed: a service that is still booting must not be reported as a
# rejection, and a second launcher must not be spawned on top of it.
_launch_process: Optional[subprocess.Popen] = None
# Whether the LAST ``ensure_started`` call ended with a launch that is still
# coming up (a launch in flight, not a failure).  The supervisor consults this so
# a slow boot never consumes the attempt budget; without it eight quick
# "still booting" returns made the supervisor give up on a service that was in
# fact about to listen.
_last_attempt_booting = False
# One key rotation per app session: after the tracking server rejected the WS
# product key and the alternative was refused as well, swapping the two back and
# forth would only hide the real cause (see ``_rotate_rejected_product_key``).
_product_key_rotated = False


def _record_launch(process: subprocess.Popen) -> None:
    """Remember the launcher we just started (see ``_launch_process``)."""

    global _launch_process
    with _lock:
        _launch_process = process


def _clear_launch() -> None:
    global _launch_process
    with _lock:
        _launch_process = None


def _launch_still_booting() -> Optional[int]:
    """The pid of a launcher this app started that has not exited, or None."""

    with _lock:
        process = _launch_process
    if process is None:
        return None
    try:
        if process.poll() is None:
            return int(process.pid)
    except Exception:  # noqa: BLE001 - a dead handle means "not booting"
        return None
    return None


def ensure_started(
    app_root: Path | str, *, wait_seconds: float = START_WAIT_SECONDS,
) -> tuple[bool, str]:
    """Start the service when it is not already listening; never raises.

    A ``False`` return means two different things, and ``_last_attempt_booting``
    separates them: a launch that is still COMING UP (keep waiting, it is not a
    failure) versus an attempt that really did not start (the vendor refused or
    died, its own log says why).
    """

    global _last_attempt_booting
    _last_attempt_booting = False
    root = find_service_root(app_root)
    if root is None:
        with _lock:
            _state.update(state="missing", note="未找到本地测谎服务目录",
                          root="", started_by_app=False, pids=[])
        LOG.info("local lie service: the package folder is not installed "
                 "next to this build, so 本地测谎 stays external")
        return False, str(read_state().get("note") or "")
    # Probe OUTSIDE the lock.  It is an HTTP health check with a 3 s timeout, and
    # holding ``_lock`` across it blocked every reader on the Tk thread (see
    # ``read_state``): the window stopped answering while the header still read
    # 正在验证在线授权.
    running, note = _probe()
    with _lock:
        if running:
            # Adopt a service THIS install launched in an earlier session: it is
            # ours by provenance, so the app stops it again on exit instead of
            # leaving an orphan that holds 8765 for ever ("the local lie service
            # is not managed any more when I close the instance").  A service the
            # operator started by hand has no marker and keeps running.
            owned = _owned_by_us(root)
            _state.update(state="running" if owned else "external", note=note,
                          root=str(root), started_by_app=owned,
                          pids=_service_pids(root) if owned else [],
                          card_rejected=False)
        else:
            booting_pid = _launch_still_booting()
            if booting_pid is None:
                # A new launch is a new validation attempt.  A previous timeout or
                # wrapper hand-off must not keep the panel at 启动失败 while this
                # attempt is genuinely booting; a real new failure latches the flag
                # again below.
                _state.update(state="starting", note="正在启动本地测谎服务…",
                              root=str(root), started_by_app=True, pids=[],
                              card_rejected=False)
    if running:
        LOG.info("local lie service: already running (%s)", note)
        # The running instance already read its config at startup, so the
        # plaintext copy is no longer needed - blank it unconditionally: no
        # credential stays on disk, not even a stale one this session does not
        # hold (memory is the only authority).
        try:
            scrub_service_credentials(app_root)
        except Exception:  # noqa: BLE001 - a scrub must never block a start
            LOG.debug("local lie service: credential scrub failed", exc_info=True)
        return True, note
    if booting_pid is not None:
        # ONE launcher at a time.  Spawning a second vendor process while the
        # first is still booting makes them collide on 8765 (the vendor binary
        # is a single-instance service), so neither listens and the panel reads
        # "本地测谎启动失败" until the operator restarts the app - the exact
        # field report after a package update, whose first start is slow
        # because Defender is still scanning the freshly copied 339 MB folder.
        with _lock:
            _state.update(
                state="starting",
                note="本地测谎服务正在启动（等待上一次启动完成）",
                root=str(root), started_by_app=True, card_rejected=False,
            )
        LOG.info(
            "local lie service: launcher pid=%s is still booting; not starting "
            "a second instance", booting_pid,
        )
        _last_attempt_booting = True
        return False, str(read_state().get("note") or "")
    LOG.info("local lie service: starting %s", root)
    # A service process left over from the previous app run (a slow shutdown, or the
    # instance the update replaced) still holds 8765 for a moment.  Launching on top
    # of it makes the vendor binary refuse to start - it is a single-instance
    # service - and the panel then reads 本地测谎启动失败 until the app is restarted.
    # Wait for that process instead of racing it, and never kill it: a service the
    # operator started by hand must keep running.
    stale_pids = _service_pids(root)
    if stale_pids:
        LOG.info(
            "local lie service: %d process(es) of the service folder are alive while "
            "8765 stays quiet; waiting for them instead of starting a second instance",
            len(stale_pids),
        )
        grace_deadline = time.monotonic() + STALE_SERVICE_GRACE_SECONDS
        answered = False
        while time.monotonic() < grace_deadline:
            time.sleep(PROBE_INTERVAL_SECONDS)
            running, note = _probe()
            if running:
                with _lock:
                    _state.update(state="external", note=note, root=str(root),
                                  started_by_app=False, pids=stale_pids,
                                  card_rejected=False)
                LOG.info("local lie service: the running instance answered (%s)", note)
                return True, note
            if not _service_pids(root):
                LOG.info("local lie service: the previous instance has exited; "
                         "starting a fresh one")
                answered = True          # reuse the flag: "stop waiting"
                break
        if not answered:
            LOG.warning(
                "local lie service: %d process(es) still hold the folder and 8765 "
                "stays quiet; starting another instance anyway", len(stale_pids),
            )
    # A config carried over from another install can name the wrong
    # authorization port; repair it before the service verifies anything.
    try:
        normalize_auth_server(root)
    except Exception:  # noqa: BLE001 - a repair must never block the start
        LOG.debug("local lie service: auth URL normalization failed", exc_info=True)
    # The vendor's coordinate-cloud startup gate samples ``roi_track_servers`` from
    # data/build_profile.json and ABORTS the start when every sampled WS handshake
    # fails.  That list still contains the retired 221.10.121.69:44444 deployment,
    # so half of every pool is dead and the first launch is a coin flip on the one
    # live host (16:02/16:04 field log).  Keep only the live deployment.
    try:
        normalize_roi_track_servers(root)
    except Exception:  # noqa: BLE001 - a repair must never block the start
        LOG.debug("local lie service: ROI-track server list repair failed", exc_info=True)
    # The updated package ships its template (placeholder) WS key; a start with that
    # text in the config fails the handshake and 8765 never listens, so it is
    # normalized to the package's own key first - whatever put the card there.
    try:
        normalize_placeholder_ws_key(root)
    except Exception:  # noqa: BLE001 - a cleanup must never block the start
        LOG.debug("local lie service: WS key normalization failed", exc_info=True)
    # A replaced vendor package ships an EMPTY card (the 2026-11 drop does), while
    # the licence reply already delivered one earlier in this session.  Put it back
    # before launching, or the service refuses to listen (no_valid_license) and the
    # panel keeps 本地测谎 enabled for a backend that cannot serve a pass.
    # Every launch rewrites the two credentials from memory: the config is
    # scrubbed as soon as the service has read it (see scrub_service_credentials),
    # so whatever an earlier run left there - a stale key, a foreign card, the
    # package's own dead constant - is never what the vendor reads.
    try:
        if rewrite_service_credentials_from_memory(app_root):
            LOG.info(
                "local lie service: card and WS key rewritten from memory before the "
                "start; they are removed again once 8765 answers",
            )
    except LocalLieServiceError as exc:
        LOG.warning("local lie service: the CT card could not be restored (%s)", exc)
    except Exception:  # noqa: BLE001 - a repair must never block the start
        LOG.debug("local lie service: credential rewrite failed", exc_info=True)
    # Do not hand the vendor a file without a usable key.  It would fall back to
    # the constant in its package, which the tracking server refuses, and the
    # attempt would burn a start cycle on a guaranteed failure (the 12:22 field
    # log: card validated, handshake denied, every retry identical).  Wait for
    # the licence heartbeat's key instead.
    if not _config_has_usable_key(app_root):
        with _lock:
            # Its own state: the panel sentence must say it is still verifying and
            # has not started, not that a launch is in flight.
            _state.update(
                state="verifying",
                note="本地测谎服务：正在校验，还未启动",
            )
        LOG.warning(
            "local lie service: the config has no usable WS key yet (card=%s); not "
            "launching the vendor, because it would fall back to the package's own "
            "refused constant - waiting for the licence heartbeat",
            _mask(normalise_credential(_state.get("online_card"))) or "none",
        )
        _last_attempt_booting = True
        return False, str(_state["note"])
    process = _launch(root)
    if process is None:
        rejected = has_online_card()      # read outside the lock (see _lock)
        with _lock:
            _state.update(state="failed", note="本地测谎服务无法启动（缺少启动器）")
            if rejected:
                # A card WAS delivered, yet the service cannot come up: on this
                # machine that means the card did not validate.
                _state["card_rejected"] = True
        _scrub_after_attempt(app_root)
        return False, str(_state["note"])
    _record_launch(process)
    # ``启动_后台.vbs`` / ``launch_background.vbs`` is only a hand-off wrapper:
    # WScript may exit successfully while the vendor executable is still
    # unpacking, validating its card and opening 8765.  Its exit is therefore
    # not evidence that the service failed.  A direct executable remains a
    # real process failure and is still reported immediately below.  Which one
    # this attempt used is decided by the process that was actually created, not
    # by the files that happen to exist in the folder.
    launched_via_script = bool(
        getattr(process, "args", None)
        and "wscript" in str(process.args[0]).lower()
    )
    wrapper_exit_logged = False
    vendor_gone_since: Optional[float] = None
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        time.sleep(PROBE_INTERVAL_SECONDS)
        running, note = _probe()
        if running:
            pids = _service_pids(root)
            _clear_launch()
            with _lock:
                _state.update(state="running", note=note, pids=pids,
                              started_by_app=True, card_rejected=False)
            LOG.info("local lie service: started (pid=%s, %s)", pids, note)
            # 8765 answers, so the vendor has read card + key: blank them again,
            # unconditionally (no credential stays in the file).
            try:
                scrub_service_credentials(app_root)
            except Exception:  # noqa: BLE001 - a scrub must never block a start
                LOG.debug("local lie service: credential scrub failed", exc_info=True)
            return True, note
        if process.poll() is not None and not launched_via_script:
            # The launcher itself exited and 8765 never opened: the service
            # refused to start (licence/WS validation, or a port already taken by
            # a leftover instance).  That IS a failure, and it is reported with
            # the exit code instead of a bare timeout.
            code = process.returncode
            _clear_launch()
            rejected = has_online_card()      # read outside the lock (see _lock)
            with _lock:
                _state.update(
                    state="failed",
                    note=f"本地测谎服务启动失败（进程已退出，代码 {code}）",
                )
                if rejected:
                    _state["card_rejected"] = True
            LOG.warning(
                "local lie service: the launcher exited (code %s) before 8765 "
                "answered; see data\\logs\\client.log in the service folder",
                code,
            )
            _scrub_after_attempt(app_root)
            return False, str(_state["note"])
        if process.poll() is not None and launched_via_script and not wrapper_exit_logged:
            wrapper_exit_logged = True
            LOG.info(
                "local lie service: launcher script finished; the vendor service now "
                "owns the start (waiting for it to open 8765)"
            )
        if process.poll() is not None and launched_via_script:
            # The wrapper handed off, but the vendor process it started is the
            # thing that must open 8765.  When it is gone too, the start has
            # already failed and waiting the rest of the 90 s budget only keeps
            # the panel at 启动中 (the 16:02 field log: the vendor died 4 s into
            # the handshake and the panel stayed "启动中" for 90 s per attempt).
            vendor_alive = bool(_service_pids(root))
            if vendor_alive:
                vendor_gone_since = None
            else:
                now = time.monotonic()
                if vendor_gone_since is None:
                    vendor_gone_since = now
                elif now - vendor_gone_since >= VENDOR_GONE_GRACE_SECONDS:
                    _clear_launch()
                    reason = _log_vendor_reason(
                        root, "local lie service: the service process exited during "
                              "startup",
                    )
                    note = "本地测谎服务启动失败（服务进程已退出"
                    note += f"：{reason}）" if reason else "）"
                    with _lock:
                        _state.update(state="failed", note=note)
                    _scrub_after_attempt(app_root)
                    return False, note
    # Still booting.  A first start after a package update has to unpack/validate a
    # 339 MB vendor folder that Windows Defender is scanning at the same time, and
    # the service validates its monthly card and does a WS handshake before it
    # listens - that can take longer than this budget.  Report "starting" (not
    # "rejected") and keep the launched process: latching a rejection here is what
    # made a slow-but-successful first start look failed until the app restarted.
    alive = process.poll() is None
    if alive:
        with _lock:
            _state.update(
                state="starting",
                note="本地测谎服务正在启动（首次启动较慢，继续等待）",
            )
        LOG.warning(
            "local lie service: still booting after %.0fs (pid=%s); keeping it and "
            "waiting instead of reporting a failure",
            wait_seconds, process.pid,
        )
        _last_attempt_booting = True
        return False, str(_state["note"])
    # The full startup budget elapsed and 8765 still did not answer.  This is a
    # real failure whether the launcher was a script or the executable itself.
    # The licence server gave us a CT card and the service still refuses to listen:
    # its own log shows 启动月卡验票失败 for an unactivated card.
    rejected = has_online_card()          # read outside the lock (see _lock)
    _clear_launch()
    with _lock:
        _state.update(state="failed", note="本地测谎服务启动超时（8765 未响应）")
        if rejected:
            _state["card_rejected"] = True
    LOG.warning("local lie service: still not answering after %.0fs",
                wait_seconds)
    _scrub_after_attempt(app_root)
    return False, str(_state["note"])


def _config_has_usable_key(app_root: Path | str) -> bool:
    """Whether the file now carries a WS key the tracking server will accept.

    A launch is only worth making when this is true: the vendor falls back to the
    constant baked into its package when the field is empty, and that constant is
    answered with ``unknown_or_deleted_product_key`` - a guaranteed failure that
    also looks like "the card is fine but the handshake is denied" in its log
    (2026-11-16 12:22 field log).
    """

    path = service_credentials_path(app_root)
    if path is None or not path.is_file():
        return False
    settings = _read_service_settings(path)
    if not isinstance(settings, dict):
        return False
    key = normalise_credential(settings.get("lie_product_key"))
    if not key or _looks_like_placeholder(key) or credential_has_cjk(key):
        return False
    package_key = _build_profile_ws_key(app_root)
    if package_key and key.casefold() == package_key.casefold():
        return False              # the dead shipped constant
    return True


def _scrub_after_attempt(app_root: Path | str) -> None:
    """Blank the plaintext credentials after a start attempt ended (any outcome).

    Never raises: the scrub is housekeeping, and a failed start must still be
    reported with its own reason.
    """

    try:
        scrub_service_credentials(app_root)
    except Exception:  # noqa: BLE001 - housekeeping only
        LOG.debug("local lie service: credential scrub failed", exc_info=True)


_supervisor_thread = None
_supervisor_stop = threading.Event()
# The selection wants 本地测谎 (_desired_active), the install root the supervisor
# uses (_desired_root), and the CT card the licence server delivered
# (_state["online_card"]).  The service is started only when the selection wants it
# AND a CT_ card exists - it is never started blindly at launch.
_desired_active = False
_desired_root: Optional[Path] = None
# Consecutive launch attempts the supervisor has spent since the selection was
# (re)applied or the service last answered.  See MAX_AUTO_START_ATTEMPTS.
_start_attempts = 0
# When the 本地测谎 selection was applied, so the first start can wait for the
# licence heartbeat instead of launching on the card an old session left behind
# (CARD_REPLY_WAIT_SECONDS).
_selected_at = 0.0


def _card_from_reply() -> str:
    """The CT card THIS session's licence reply delivered, or ""."""

    state = read_state()
    if state.get("card_denied"):
        return ""
    return str(state.get("online_card") or "").strip()


_heartbeat_wait_logged = False
_card_wait_thread: Optional[threading.Thread] = None


def _schedule_card_wait_expiry(root: Path, seconds: float) -> None:
    """Re-check the selection once the heartbeat wait window has elapsed.

    Without this the "wait for a complete heartbeat, then start" rule would leave
    the service off for ever on a machine whose licence request never answers:
    nothing else calls ``supervisor_set_active`` on a timer.  The thread is a
    daemon and does nothing when the selection left 本地测谎 or a reply arrived.
    """

    global _card_wait_thread
    with _lock:
        if _card_wait_thread is not None and _card_wait_thread.is_alive():
            return
        thread = threading.Thread(
            target=_card_wait_expiry_job, args=(root, float(seconds)),
            name="local-lie-card-wait", daemon=True,
        )
        _card_wait_thread = thread
    thread.start()


def _card_wait_expiry_job(root: Path, seconds: float) -> None:
    if _supervisor_stop.wait(max(0.1, seconds)):
        return
    with _lock:
        desired = _desired_active
    if not desired:
        return
    if _card_from_reply():
        # A reply arrived meanwhile; it has already started the service.
        return
    LOG.info(
        "local lie service: the heartbeat did not deliver a CT card in time; "
        "re-evaluating the start with the card already in the service config"
    )
    supervisor_set_active(True, root)


def _supervisor_job(root, interval: float) -> None:
    """Keep 本地测谎 running while the selection wants it.

    The vendor's own start is a GATE over its compiled coordinate-cloud WS
    candidates (see ``VENDOR_GONE_GRACE_SECONDS``): while the deployment's WS
    service does not answer, EVERY attempt fails the same way, which is the
    2026-11-15 field log (8 attempts in 2.5 minutes, all 坐标云启动握手失败).  Four
    rules keep that honest and recoverable:

      * a launch that is still BOOTING is never counted as a failed attempt
        (eight quick "still booting" returns used to spend the whole budget on a
        service that was about to listen),
      * a real failure retries every ``START_RETRY_FAST_SECONDS`` while the
        attempt budget lasts,
      * when the budget is spent the panel flips to 本地测谎：启动失败 (the vendor's
        reason stays in the log), and the supervisor keeps watching and retrying
        every ``SLOW_RETRY_SECONDS`` - it never quits, so a deployment that
        recovers is adopted and the panel turns 启动成功 without a radio toggle
        (the previous version returned from this thread and required reselecting
        本地测谎),
      * a service that answers resets the budget and clears the rejection latch.
    """

    global _start_attempts
    gave_up_logged = False
    # Once the budget has been spent the panel keeps saying 启动失败 during later
    # retry batches (a genuine boot still paints 启动中 through ensure_started), so
    # the sentence stops flickering between 启动中 and 启动失败 every few minutes.
    gave_up_once = False
    while not _supervisor_stop.is_set():
        try:
            if is_running():
                # The service answers, so it is up - a rejection latched by an
                # earlier attempt must not keep the panel at 本地测谎启动失败.
                # ``ensure_started`` is deliberately not called here (nothing to
                # start), which is exactly why the latch has to be cleared here.
                note_service_answering()
                with _lock:
                    _start_attempts = 0
                gave_up_logged = False
                gave_up_once = False
            else:
                with _lock:
                    attempts = int(_start_attempts or 0)
                if attempts >= MAX_AUTO_START_ATTEMPTS:
                    if not gave_up_logged:
                        gave_up_logged = True
                        gave_up_once = True
                        reason = _log_vendor_reason(
                            root,
                            f"local lie service: {attempts} attempt(s) failed",
                        )
                        note = "本地测谎服务启动失败"
                        note += f"（{reason}）" if reason else "（见 data\\logs\\client.log）"
                        with _lock:
                            _state.update(state="failed", note=note)
                        LOG.warning(
                            "local lie service: %d attempt(s) failed; keeping watch "
                            "and retrying every %.0fs", attempts, SLOW_RETRY_SECONDS,
                        )
                    if _supervisor_stop.wait(SLOW_RETRY_SECONDS):
                        return
                    with _lock:
                        _start_attempts = 0
                    gave_up_logged = False
                    continue
                ok, note = ensure_started(root)
                if not ok and not _supervisor_stop.is_set():
                    if _last_attempt_booting:
                        # A launch is still coming up.  That is not a failure and
                        # must not consume the attempt budget; the next probe
                        # decides whether it listened.
                        LOG.info(
                            "local lie service: the start is still booting (%s); "
                            "waiting for it", note,
                        )
                    else:
                        with _lock:
                            _start_attempts = attempts + 1
                        attempt_number = attempts + 1
                        reason = _log_vendor_reason(
                            root,
                            f"local lie service: attempt {attempt_number}/"
                            f"{MAX_AUTO_START_ATTEMPTS} failed",
                        )
                        # An auth verdict about the PRODUCT KEY cannot be retried
                        # into success, and restarting the vendor only repeats it.
                        # Swap to the machine's other key once, then retry.
                        _rotate_rejected_product_key(root, reason)
                        short = _short_reason(reason or str(note))
                        if attempt_number >= MAX_AUTO_START_ATTEMPTS:
                            # Budget spent: state the failure NOW, with the reason,
                            # instead of leaving the panel on 启动中 for ever (the
                            # 09:01 field log ended exactly like that).
                            gave_up_once = True
                            note_text = "本地测谎服务启动失败"
                            note_text += f"（{short}）" if short else "（见 data\\logs\\client.log）"
                            with _lock:
                                _state.update(state="failed", note=note_text)
                        elif gave_up_once:
                            # A later retry batch: keep the honest failed state and
                            # only refresh the counter.
                            with _lock:
                                note_text = "本地测谎服务启动失败"
                                note_text += f"（{short}）" if short else ""
                                _state.update(state="failed", note=note_text)
                        else:
                            with _lock:
                                _state.update(
                                    state="starting",
                                    note=("启动未成功（第 %d/%d 次），正在重试%s" % (
                                        attempt_number, MAX_AUTO_START_ATTEMPTS,
                                        f"：{short}" if short else "…",
                                    )),
                                )
                        LOG.warning(
                            "local lie service: attempt %d/%d did not come up (%s); "
                            "retrying in %.0fs", attempt_number,
                            MAX_AUTO_START_ATTEMPTS, note, START_RETRY_FAST_SECONDS,
                        )
                        if _supervisor_stop.wait(START_RETRY_FAST_SECONDS):
                            return
                        continue
        except Exception:  # noqa: BLE001 - supervision never dies
            LOG.warning("local lie service: supervisor start failed", exc_info=True)
        _supervisor_stop.wait(interval)


def _owner_marker_path() -> Path:
    return install_root() / "work" / OWNER_MARKER_NAME


def _remember_ownership(root: Path) -> None:
    """Record that THIS install launched the service, across restarts.

    Without it, a service left behind by the previous run was found answering on
    the next start, classified ``external`` (``started_by_app`` False), and from
    then on the app never stopped it again - the operator's "when I close the
    instance the local lie service is not managed any more".  The marker keeps the
    ownership decision; a service the operator started by hand never gets one, so
    it is still never killed.
    """

    try:
        path = _owner_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "root": str(root),
                "started": time.time(),
                "pids": _service_pids(root),
            }, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - ownership bookkeeping is best effort
        LOG.debug("local lie service: ownership marker could not be written",
                  exc_info=True)


def _release_ownership() -> None:
    try:
        _owner_marker_path().unlink(missing_ok=True)
    except OSError:
        LOG.debug("local lie service: ownership marker could not be removed",
                  exc_info=True)


def _owned_by_us(root: Path) -> bool:
    """True when a service THIS install launched is still running."""

    try:
        data = json.loads(_owner_marker_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no marker means "not ours"
        return False
    try:
        recorded = Path(str(data.get("root") or "")).resolve()
        if os.path.normcase(str(recorded)) != os.path.normcase(str(root.resolve())):
            return False
    except OSError:
        return False
    return bool(_service_pids(root))


def note_service_answering(note: str = "") -> None:
    """A probe proved 127.0.0.1:8765 answers: mark the service up.

    ``card_rejected`` answers "did the CT card validate on this machine?", and it
    is latched when a start attempt fails while a card is known.  A service that
    ANSWERS has evidently validated its card, so the latch is stale by
    definition.  Only a freshly launched success used to clear it, and the
    supervisor skips ``ensure_started`` entirely once 8765 is quiet-free - so a
    single failed attempt (a slow first start, a leftover instance holding the
    port) left the panel reading 本地测谎启动失败 while passes were being served.
    """

    text = str(note or "").strip() or "127.0.0.1:8765 已响应"
    with _lock:
        previous = str(_state.get("state") or "idle")
        started_by_app = bool(_state.get("started_by_app"))
        root_text = str(_state.get("root") or "")
        _state.update(
            state="running" if started_by_app else "external",
            note=text,
            card_rejected=False,
        )
    if started_by_app and root_text:
        # A start THIS app performed: remember the ownership so the next run (and
        # this run's shutdown) knows the service belongs to the app.
        _remember_ownership(Path(root_text))
    if previous not in ("running", "external"):
        LOG.info(
            "local lie service: 8765 answers, so the service is up (%s -> %s)",
            previous, "running" if started_by_app else "external",
        )


def config_card(app_root: Path | str | None = None) -> str:
    """The CT_ card the service's OWN config currently carries, or "".

    This is what the service will validate at startup, so it is the truth about
    whether 本地测谎 can run on this machine right now.  It matters because the
    memory card comes from a licence reply: replacing the vendor package (or a
    reply that carries no device document) leaves memory and config disagreeing,
    and the panel used to look "enabled" while the service had no card at all.
    """

    root = Path(app_root) if app_root is not None else install_root()
    path = service_credentials_path(root)
    if path is None or not path.is_file():
        return ""
    settings = _read_service_settings(path)
    if not isinstance(settings, dict):
        return ""
    card = normalise_credential(settings.get("partner_license_code"))
    return card if card.upper().startswith("CT_") else ""


def current_card(app_root: Path | str | None = None) -> str:
    """The CT_ card this machine may start the service with, or "".

    Precedence, and the ONE rule for 本地测谎 access:

    1. a licence reply that ended in ``note_card_absent()`` wins outright - the
       activation server said this code has no CT card, so a card cached in the
       service config (written by an earlier session, or left by a replaced
       package) must NOT keep the backend selectable;
    2. otherwise the in-memory card from the last reply;
    3. otherwise the card already written into the service config, which the
       service validates itself - this is what keeps a working install usable
       when no reply has arrived yet in this session.
    """

    state = read_state()
    if state.get("card_denied"):
        return ""
    card = str(state.get("online_card") or "").strip()
    return card or config_card(app_root)


def has_online_card() -> bool:
    """Whether a usable CT_ card exists (licence reply OR the service config)."""

    return current_card().upper().startswith("CT_")


def card_known() -> bool:
    """Whether it is already decided if this machine has 本地测谎 access.

    Before the first reply the desktop knows nothing, so the 本地测谎 selection must
    not be locked out merely because the heartbeat has not answered yet.  A reply
    that carries the device document is authoritative either way: a card sets
    ``online_card``, a reply without one calls note_card_absent().  A card already
    written into the service config decides it too - the service validates that
    file itself, so a silent or cardless reply must not grey a working install.
    """

    state = read_state()
    if state.get("card_checked"):
        return True
    return bool(config_card())


def note_card_absent() -> None:
    """The licence reply carries no CT card: this code has no 本地测谎 access.

    Authoritative, and it outranks a card cached in the service config: the reply
    is the server saying "no card on this code", so the backend is greyed even if
    an earlier session (or a replaced package) left a CT_ card in
    ``data/ct_login_config.json``.  A later reply that carries a card clears the
    verdict again.

    The card file itself is left alone - it belongs to the vendor package, and only
    a reply that PROVES a card may write it - but a service THIS app started is
    stopped, so nothing keeps billing on a credential the server withdrew.
    """

    with _lock:
        had = bool(str(_state.get("online_card") or ""))
        _state["card_checked"] = True
        _state["card_denied"] = True
        _state["online_card"] = ""
    cached = config_card()
    if had or cached:
        LOG.warning(
            "local lie service: the licence reply carries no CT card - 本地测谎 is "
            "greyed%s",
            " (a card cached in the service config is ignored)" if cached else "",
        )
    else:
        LOG.info("local lie service: this activation code carries no CT card")
    try:
        stop_started_service()
    except Exception:  # noqa: BLE001 - a stop must never break a licence reply
        LOG.debug("local lie service: could not stop after the card was withdrawn",
                  exc_info=True)



def supervisor_set_active(active: bool, app_root=None) -> None:
    """Turn the pre-start supervisor on or off with the 本地测谎 selection.

    The operator's rule has two halves: the service is pre-started while 本地测谎
    is selected (never lazily at a lie window), but it must NOT be started blindly
    - the first licence reply decides.  Without a CT_ card in that reply there is
    nothing to activate, so the service stays off and the panel says so.
    """

    global _supervisor_thread, _desired_active, _desired_root, _start_attempts
    global _selected_at
    if not active:
        _supervisor_stop.set()
        with _lock:
            _supervisor_thread = None
            _desired_active = False
            _state["card_rejected"] = False
            _start_attempts = 0
            _selected_at = 0.0
        stop_started_service()
        LOG.info("local lie service: pre-start disabled (selection left 本地测谎)")
        return
    root = Path(app_root) if app_root is not None else install_root()
    # ``app_root`` is the ASSISTANT's folder (that is what every caller passes,
    # including the CT-card path ``note_online_card``).  The state's ``root`` field
    # is the SERVICE folder, because that is the value ``stop_started_service``
    # verifies pids against - recording the assistant folder there made the stop
    # look at the assistant's own executable.  Resolve it once, here.
    service_root = find_service_root(root)
    if service_root is None:
        LOG.info(
            "local lie service: no packaged service folder under %s, so 本地测谎 "
            "stays external and nothing is ever started or stopped here", root,
        )
    with _lock:
        newly_selected = not _desired_active
        _desired_active = True
        _desired_root = root
        running = _supervisor_thread is not None and _supervisor_thread.is_alive()
        if newly_selected:
            # A new selection is a new start budget: the attempt counter and the
            # heartbeat wait both restart here.
            _start_attempts = 0
            _selected_at = time.monotonic()
        selected_at = float(_selected_at or 0.0)
    if not has_online_card():
        LOG.info(
            "local lie service: 本地测谎 selected, but the licence server has not "
            "delivered a CT card yet - the service is not started until it does"
        )
        return
    # Operator's rule (2026-11-13): wait for a complete heartbeat, take the card
    # from THAT reply, then start.  A CT_ card sitting in the vendor config may be
    # a leftover from an earlier session, and starting on it is what produced the
    # 16:02 start attempts that had to be repeated.  ``note_online_card`` calls
    # back into this function the moment a validated reply delivers the card, so
    # the wait costs nothing in the normal case; if no reply arrives at all the
    # config card is still used, so an offline install keeps working.
    if not _card_from_reply():
        waited = time.monotonic() - selected_at if selected_at else 0.0
        if waited < CARD_REPLY_WAIT_SECONDS:
            global _heartbeat_wait_logged
            if not _heartbeat_wait_logged:
                _heartbeat_wait_logged = True
                LOG.info(
                    "local lie service: 本地测谎 selected; waiting up to %.0fs for "
                    "the licence heartbeat to deliver the CT card before starting",
                    CARD_REPLY_WAIT_SECONDS,
                )
            # The panel says 本地测谎服务: 等待授权信息 while this gate holds: nothing
            # is launched on a card from an older session.
            with _lock:
                if str(_state.get("state") or "idle") not in ("running", "external"):
                    _state.update(
                        state="waiting", note="等待授权信息（许可证心跳）",
                        root=str(service_root or ""), started_by_app=False,
                    )
            _schedule_card_wait_expiry(root, CARD_REPLY_WAIT_SECONDS - waited + 1.0)
            return
        LOG.warning(
            "local lie service: no licence reply delivered a CT card within %.0fs; "
            "starting with the card already in the service config",
            CARD_REPLY_WAIT_SECONDS,
        )
    _heartbeat_wait_logged = False
    # The panel must show 本地测谎服务：正在启动，还未成功 the moment the radio is
    # clicked, not one supervisor iteration later: the state is set here, on the
    # caller's thread, and ``ensure_started`` then only moves it on to running or
    # failed.  A service that is already answering keeps its up state.
    with _lock:
        already_up = str(_state.get("state") or "idle") in ("running", "external")
        if not already_up:
            _state.update(
                state="starting", note="正在启动本地测谎服务…",
                root=str(service_root or ""), started_by_app=service_root is not None,
            )
    if running:
        return
    # A FRESH supervisor is a fresh start budget.  Without this reset the old
    # counter survived the previous thread, so a supervisor that had exhausted its
    # eight attempts made every later start give up immediately - the field
    # workaround was "switch to 云端测谎 and switch back".
    with _lock:
        _start_attempts = 0
        _selected_at = time.monotonic()
    _start_supervisor_thread(root)


def _start_supervisor_thread(root: Path) -> None:
    global _supervisor_thread
    with _lock:
        if _supervisor_thread is not None and _supervisor_thread.is_alive():
            return
        _supervisor_stop.clear()
        _supervisor_thread = threading.Thread(
            target=_supervisor_job, args=(root, SUPERVISE_INTERVAL_SECONDS),
            name="local-lie-supervisor", daemon=True,
        )
        _supervisor_thread.start()
    LOG.info("local lie service: pre-start enabled (本地测谎 + CT card present)")


def note_online_card(card: str) -> None:
    """Record the CT card from the licence reply and pre-start if it is wanted.

    This is the moment the operator asked for: a *validated* reply has delivered
    the card, so the service may be started.  The call goes through
    ``supervisor_set_active`` (not the thread starter directly) so the whole gate
    applies - the "wait for the heartbeat" branch is satisfied by definition here,
    a previously exhausted start budget is reset, and the panel state is set on
    this thread.  This is what re-arms the service after ``MAX_AUTO_START_ATTEMPTS``
    gave up.
    """

    global _start_attempts, _selected_at
    text = str(card or "").strip()
    with _lock:
        if text:
            _state["online_card"] = text
            # A reply that carries a card overrules an earlier "no card" verdict.
            _state["card_denied"] = False
        # A licence reply reached us, so "does this code carry a CT card" is now
        # answered either way - the 本地测谎 selection may be greyed on the truth.
        _state["card_checked"] = True
        desired = _desired_active
        root = _desired_root
        if text:
            # Fresh information: a new start budget, and the heartbeat wait is
            # over for this selection.
            _start_attempts = 0
            _selected_at = time.monotonic()
    if not text:
        return
    if desired:
        LOG.info("local lie service: the CT card arrived; pre-starting the service")
        supervisor_set_active(True, root or install_root())


def stop_supervisor() -> None:
    """Application shutdown: stop supervising and stop what we started."""

    _supervisor_stop.set()

def start_in_background(
    app_root: Path | str | None = None, *, attempts: int = 3,
) -> threading.Thread:
    """Start the service without blocking the caller (the UI thread).

    Retries: a single attempt is not enough in the field.  The service can exit
    on its first launch (licence/WS being provisioned), and a folder that appears
    a second later (an install finishing) must not mean "the user has to start it
    by hand".  Each attempt re-resolves the folder and logs at WARNING when it
    ends without a listening service, so the running log always says why.
    """

    def job() -> None:
        root = Path(app_root) if app_root is not None else install_root()
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                ok, note = ensure_started(root)
            except Exception:  # noqa: BLE001 - startup must never break the app
                LOG.warning("local lie service: start attempt %d failed",
                            attempt, exc_info=True)
                ok, note = False, "启动异常"
            if ok:
                return
            if is_running():
                return
            if attempt < attempts:
                LOG.info(
                    "local lie service: attempt %d/%d did not get 8765 listening "
                    "(%s); retrying in %ds", attempt, attempts, note,
                    int(RETRY_BACKOFF_SECONDS),
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
        LOG.warning(
            "local lie service: 8765 is still not listening after %d attempt(s) - "
            "see the service's own data\\logs\\client.log", attempts,
        )

    thread = threading.Thread(target=job, name="local-lie-service", daemon=True)
    thread.start()
    return thread


def stop_started_service() -> int:
    """Stop the service IF this app started it; returns how many were stopped.

    Only PATH-VERIFIED processes are killed.  The old code preferred the pid list
    recorded at launch, and those pids belong to the launcher script, which exits
    immediately - a Windows pid can be reused within seconds, so
    ``os.kill(recorded_pid)`` could terminate an unrelated process (a freshly
    started instance of this very app is the worst case, and it looks exactly like
    a flash close).  The stop now also WAITS for the vendor to really exit, so the
    next start finds 8765 free.
    """

    with _lock:
        started = bool(_state.get("started_by_app"))
        root_text = str(_state.get("root") or "")
    if not started or not root_text:
        LOG.info("local lie service: left running (this app did not start it)")
        return 0
    root = Path(root_text)
    pids = _service_pids(root)
    if not pids:
        # Nothing was resolved, so nothing is killed.  The old fallback ran
        # ``taskkill /F /PID`` for every process matching a BINARY NAME, and a
        # folder that resolved to the install root made that name the app's own
        # image: the assistant terminated itself with no traceback, no dump and no
        # log line - the operator's flash close.  A pid is only ever killed when the
        # path of its executable proves it lives in the service folder.
        LOG.info(
            "local lie service: no path-verified process of %s to stop; nothing "
            "was killed", root,
        )
        _release_ownership()
        with _lock:
            _state.update(state="idle", note="本地测谎服务已随程序停止",
                          started_by_app=False, pids=[])
        return 0
    stopped = 0
    me = os.getpid()
    for pid in pids:
        if int(pid) == me:
            # Belt and braces: _service_pids already excludes this process, and a
            # SIGTERM here would end the app with no trace at all.
            LOG.error("local lie service: refusing to stop pid %s (this process)", pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except OSError:
            LOG.debug("local lie service: pid %s already gone", pid)
    # Wait for the exit: 8765 must be free before a new instance may start, and
    # the vendor's own files must not be written under it.
    deadline = time.monotonic() + SERVICE_STOP_WAIT_SECONDS
    remaining = _service_pids(root)
    while remaining and time.monotonic() < deadline:
        time.sleep(PROBE_INTERVAL_SECONDS)
        remaining = _service_pids(root)
    if remaining:
        LOG.warning(
            "local lie service: %d process(es) of %s are still alive %.1fs after the "
            "stop (pids=%s); the next start may be refused",
            len(remaining), root, SERVICE_STOP_WAIT_SECONDS,
            ", ".join(str(pid) for pid in remaining),
        )
    _release_ownership()
    with _lock:
        _state.update(state="idle", note="本地测谎服务已随程序停止",
                      started_by_app=False, pids=[])
    LOG.info(
        "local lie service: stopped %d process(es) with the app (folder=%s, "
        "pids=%s, this pid=%s)", stopped, root,
        ", ".join(str(pid) for pid in pids) or "none", me,
    )
    return stopped


def note_failure(reason: str) -> None:
    """Record a failure the UI just hit, so the hint line stops saying "ready"."""

    text = str(reason or "").strip() or "本地测谎服务不可用"
    with _lock:
        _state.update(state="failed", note=text)


def service_credentials_path(app_root: Path | str) -> Optional[Path]:
    """``data/ct_login_config.json`` of the packaged service, or None."""

    root = find_service_root(app_root)
    if root is None:
        return None
    return root / "data" / "ct_login_config.json"


def _mask(secret: str) -> str:
    text = str(secret or "")
    if len(text) <= 10:
        return "***" if text else ""
    return text[:6] + "*" * (len(text) - 10) + text[-4:]


# The CT package's own settings file, in the encodings it is actually shipped in.
# The 2026-11 package (build_profile.client_version 1.0.0, exe AppHostd83e.exe)
# ships ``data/ct_login_config.json`` as **GBK/CP936**, because the vendor writes
# it with a locale ``open()`` on a Chinese Windows: its placeholders are the
# Chinese words 你的月卡号 / 你的坐标云密钥, and 0xC4 is not valid UTF-8.  Our
# writers READ it strictly as UTF-8 before, so ``json.loads`` raised
# ``UnicodeDecodeError`` (a ValueError), the ``except`` branch fell back to an
# empty dict, and the next write replaced the WHOLE vendor document with one key -
# losing license_server_url, lie3_backend, the vm_pierce_* switches, the ORT
# endpoint and the http/ws fallback flags.  Reading through the fallback chain and
# writing ASCII-only JSON (escaped Unicode is identical under UTF-8 and CP936)
# keeps the file readable for the service whichever code page it uses.
_SERVICE_CONFIG_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1")
# A value that is still the vendor's template cannot be used as a credential.
_PLACEHOLDER_HINTS = ("你的", "填", "your", "xxxx", "change", "todo")
# Characters a CJK keyboard/IME can silently inject into a pasted credential:
# zero-width joiners/spaces and a BOM, which make the value compare unequal even
# though it looks identical on screen.
_INVISIBLE_CHARACTERS = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None
)


def normalise_credential(value: object) -> str:
    """Normalise a pasted CT card / product key to the ASCII the server expects.

    A credential typed through a Chinese IME can arrive with FULL-WIDTH forms -
    a full-width hyphen ``－`` (U+FF0D) instead of ``-``, full-width letters and
    digits, and an ideographic space - and then the tracking server answers
    ``unknown_or_deleted_product_key`` for a value that looks correct on screen.
    NFKC folds every full-width form back to ASCII, and invisible characters are
    dropped, so such a value becomes usable instead of silently failing.
    """

    text = str(value or "").translate(_INVISIBLE_CHARACTERS)
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        character for character in text
        if not character.isspace() and character.isprintable()
    )


def credential_has_cjk(value: object) -> bool:
    """Whether a normalised credential still contains a non-ASCII character."""

    return any(ord(character) > 0x7F for character in normalise_credential(value))


def _looks_like_placeholder(value: object) -> bool:
    """Whether a config value is still the vendor's template text."""

    text = normalise_credential(value)
    if not text:
        return True
    lowered = text.casefold()
    return any(hint in lowered or hint in text for hint in _PLACEHOLDER_HINTS)


def _read_service_settings(path: Path) -> Optional[dict]:
    """Parse the package's config whatever encoding it was shipped in.

    ``None`` means "the document could not be understood": callers must then
    leave the file alone rather than overwrite settings they never read.
    """

    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    text: Optional[str] = None
    for encoding in _SERVICE_CONFIG_ENCODINGS:
        try:
            text = blob.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_service_settings(path: Path, settings: dict) -> None:
    """Write the config as ASCII-only JSON, so any code page parses it.

    No BOM (the service's reader is strict about UTF-8) and ``ensure_ascii``:
    escaped Unicode means the bytes are valid UTF-8 *and* valid GBK, so the
    vendor's locale reader and our own reader see the same document.
    """

    Path(path).write_text(
        json.dumps(settings, ensure_ascii=True, indent=2), encoding="utf-8"
    )


def _load_service_settings(path: Path, *, what: str) -> Optional[dict]:
    """Read the config, or record why the caller must not write it."""

    settings = _read_service_settings(path)
    if settings is None:
        LOG.warning(
            "local lie service: %s is not readable as JSON (any of %s); "
            "leaving it untouched so the package's own settings survive",
            path, "/".join(_SERVICE_CONFIG_ENCODINGS),
        )
        return None
    return settings


def _package_ws_key_or_empty(app_root: Path | str, settings: dict) -> str:
    """The key this file can handshake with today ("" when it has none).

    DELIBERATELY returns only the field's own value: the constant this package
    ships (``LIE-PLANET-001``) is refused by the tracking server with
    ``unknown_or_deleted_product_key`` (measured 2026-11-16), so it is not a
    usable substitute for a placeholder.  Kept for callers that want to know
    whether the config has a key at all.
    """

    current = normalise_credential(settings.get("lie_product_key"))
    return "" if _looks_like_placeholder(current) else current


def normalize_roi_track_servers(app_root: Path | str) -> bool:
    """Keep only live deployments in the profile's ROI-track pool.

    ``build_profile.json`` ships::

        "roi_track_servers": [
            {"host": "221.10.121.69", "ws_ports": [44444], "label": "四川"},
            {"host": "117.50.223.113", "ws_ports": [8888],  "label": "华北"}
        ]

    ``221.10.121.69`` has been retired (the operator: "this server is stopped, we
    shouldn't use it"), so every start wasted a handshake on a dead host.

    **This repair does NOT change the vendor's startup gate.**  That gate works on a
    candidate list COMPILED into the vendor binary: its own log says
    ``锁定 221+117 共 2 WS 口`` even though both copies of this file list only the
    live host, so a start still probes the retired endpoint and still depends on
    ``117.50.223.113:8888`` answering.  Only the vendor (or the deployment) can
    change that list.  What this function keeps is the *declared* deployment: the
    runtime WS client that does read the profile must not be pointed at a host that
    no longer exists.

    The document exists TWICE in the package: ``data\\build_profile.json`` and the
    frozen application's own ``_internal\\CT\\data\\build_profile.json``.  Both are
    trimmed (the vendor resolves ``CT/data/...`` inside its bundle), keeping only
    entries for the live deployment and leaving their ports and labels untouched.
    Returns True when any changed.
    """

    live = _live_endpoint_host()
    if not live:
        return False
    folder = Path(app_root) / SERVICE_FOLDER
    changed = False
    for relative in (
        Path("data") / "build_profile.json",
        Path("_internal") / "CT" / "data" / "build_profile.json",
    ):
        changed = _trim_roi_track_pool(folder / relative, live) or changed
    return changed


def _trim_roi_track_pool(path: Path, live: str) -> bool:
    """Keep only *live* in one ``build_profile.json``'s ROI-track pool."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        profile = json.loads(raw)
    except ValueError:
        LOG.warning(
            "local lie service: %s is not valid JSON; the ROI-track pool is left "
            "untouched", path,
        )
        return False
    if not isinstance(profile, dict):
        return False
    pool = profile.get("roi_track_servers")
    if not isinstance(pool, list) or not pool:
        return False
    kept = [
        entry for entry in pool
        if isinstance(entry, dict) and str(entry.get("host") or "").strip() == live
    ]
    dropped = [
        str(entry.get("host") or "?") for entry in pool
        if isinstance(entry, dict) and str(entry.get("host") or "").strip() != live
    ]
    if not kept:
        # Nothing for the live deployment: add it rather than leaving the pool empty
        # (an empty pool makes the vendor's gate fail every time).
        kept = [{"host": live, "ws_ports": [8888], "label": "华北", "region": "cn"}]
    if not dropped and len(kept) == len(pool):
        return False
    profile["roi_track_servers"] = kept
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False,
        ) as handle:
            json.dump(profile, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError:
        LOG.warning(
            "local lie service: could not rewrite %s; the retired ROI-track host "
            "may still be probed at startup", path, exc_info=True,
        )
        return False
    LOG.warning(
        "local lie service: ROI-track pool limited to the live deployment %s in %s "
        "(dropped retired %s)", live, path.name, ", ".join(dropped) or "none",
    )
    return True


def normalize_placeholder_ws_key(app_root: Path | str) -> bool:
    """Give the service a WS key that the tracking server still accepts.

    Called on the way into every start, before the vendor process is launched, so
    the FIRST handshake already uses a live key instead of failing once and
    retrying.  Two cases need it:

    * a template/empty ``lie_product_key`` (the operator pasted the CT_IN card by
      hand, following the vendor doc, and no licence reply has arrived yet);
    * the CONSTANT this package ships (``LIE-PLANET-001``).  Measured live on
      2026-11-16 against ws://117.50.223.113:8888: the delivered heartbeat key
      answers ``auth=True quota_left=100``, while that constant is refused with
      ``unknown_or_deleted_product_key``.  It is therefore NOT a usable fallback
      and is only written when the heartbeat has delivered nothing at all (a
      machine that has never had a licence reply cannot do better).

    Any other real key is left alone - it may be the partner-issued key.

    Returns True when the file changed.
    """

    path = service_credentials_path(app_root)
    if path is None or not path.is_file():
        return False
    settings = _read_service_settings(path)
    if settings is None:
        return False
    raw_current = str(settings.get("lie_product_key") or "")
    current = normalise_credential(raw_current)
    package_key = _build_profile_ws_key(app_root)
    placeholder = _looks_like_placeholder(current)
    shipped_constant = bool(
        package_key and current and current.casefold() == package_key.casefold()
    )
    # A key that still contains a Chinese character can never authenticate: the
    # server answers unknown_or_deleted_product_key for it, exactly like a
    # revoked constant.
    unusable = credential_has_cjk(current)
    if not placeholder and not shipped_constant and not unusable:
        # A stored value that differs from its own normalised form (full-width
        # characters from a Chinese IME, a zero-width space) is unusable as-is;
        # rewrite it before the vendor reads it.
        if current and current != raw_current:
            settings["lie_product_key"] = current
            try:
                _write_service_settings(path, settings)
            except OSError as exc:
                LOG.warning(
                    "local lie service: the WS key could not be normalised (%s)", exc
                )
                return False
            LOG.warning(
                "local lie service: the stored WS product key contained full-width "
                "or invisible characters; normalised it to %s", _mask(current),
            )
            return True
        return False
    replacement = ""
    try:
        from auto_lie_secret import get_server_secret

        delivered = normalise_credential(get_server_secret())
    except Exception:  # noqa: BLE001 - the heartbeat key is optional here
        delivered = ""
    if delivered and delivered.casefold() != current.casefold():
        replacement = delivered
    # NOTE: the constant this package ships is deliberately NOT used as a
    # fallback.  The tracking server answers
    # ``{'type': 'auth_denied', 'message': 'unknown_or_deleted_product_key'}`` for
    # it (measured 2026-11-16), so writing it converts "not configured yet" into
    # "permanently failing": exactly the field state
    # ``"partner_license_code": "CT_IN-…", "lie_product_key": "LIE-PLANET-001"``.
    # Without a delivered key the field is left empty and the next start, after
    # the licence heartbeat arrives, fills it.
    if not replacement:
        return False          # nothing better to write: leave the file alone
    settings["lie_product_key"] = replacement
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the WS key could not be written (%s)", exc)
        return False
    reason = ("non-ASCII" if unusable else
              "shipped-constant" if shipped_constant else "placeholder")
    LOG.warning(
        "local lie service: the %s WS key in %s was replaced by %s so the startup "
        "handshake can pass",
        reason, path.name, _mask(replacement),
    )
    return True


def _build_profile_ws_key(app_root: Path | str) -> str:
    """The WS product key the service package itself ships, or ""."""

    root = find_service_root(app_root)
    if root is None:
        return ""
    path = root / "data" / "build_profile.json"
    if not path.is_file():
        return ""
    profile = _read_service_settings(path)
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("ws_product_key") or "").strip()


def _product_key_writable(app_root: Path | str, settings: dict) -> bool:
    """Whether the key delivered at heartbeat may become the local WS key.

    There are TWO separate credentials on this service and they must never be
    swapped (vendor template ``data/ct_login_config.json.example``):

      * ``partner_license_code`` - the CT_IN 月卡 card, verified on port 8004;
      * ``lie_product_key``      - the coordinate-cloud product key the RoiTrack
        WS handshakes with.

    Measured against the REAL tracking server (117.50.223.113:8888, probe run on
    2026-11-16): the key the heartbeat delivers (``auto_lie.secret_key`` /
    ``tx_key``, here ``tw-x87…4323``) handshakes with ``auth=True quota_left=100``,
    while the constant this package ships (``LIE-PLANET-001``) is answered with
    ``unknown_or_deleted_product_key``.  So:

      * an empty or placeholder field is filled - with the DELIVERED key, because
        it is the live one;
      * a field that still holds the PACKAGE's own constant is replaced by the
        delivered key: that constant is shared by every install and the server
        has already deleted it, so keeping it guarantees a failed handshake;
      * any other real key is KEPT - it may be the partner-issued key, and the
        heartbeat secret belongs to the remote backend's cloud.

    The caller's key is written only when it is non-empty; see write_credentials.
    """

    current = normalise_credential(settings.get("lie_product_key"))
    if _looks_like_placeholder(current):
        return True                       # nothing usable yet: any real key wins
    package_key = _build_profile_ws_key(app_root)
    if package_key and current.casefold() == package_key.casefold():
        # The shipped constant: revocable, and already refused by the server.
        return True
    return False                          # a real, working key is never replaced



# The CT card is authorized on this port only (vendor doc: "CT_IN 月卡：117:8004
# 验票，product_id=ct_lie3_partner_in").  Pinning it in code keeps a config that
# carries an old or hand-edited port from silently breaking the card check, whose
# symptom is a service that starts and never listens.
AUTH_SERVER_PORT = 8004
AUTH_SERVER_FILE = "login_config.json"



# The card format the vendor issues: CT_IN-XXXX-XXXX-XXXX-XXXX (27 characters).
CARD_PATTERN = re.compile(r"^CT_[A-Z0-9]+(?:-[A-Z0-9]+){3,4}$")


def canonical_card(value: object) -> str:
    """The first well-formed CT card inside *value* ("" when there is none).

    The server stores the card in a 30-character field, so a value pasted twice
    ("CT_IN-.... CT_I") or padded arrives truncated.  Keeping the first clean
    token makes the config usable and surfaces the truncation (masked) instead of
    silently writing a broken code.

    The value is normalised first, so a card typed through a Chinese IME with
    full-width letters/hyphens (``ＣＴ＿ＩＮ－…``) is accepted as the ASCII card
    instead of being written as an unusable string.
    """

    raw = normalise_credential(value)
    if not raw:
        return ""
    if credential_has_cjk(raw):
        LOG.warning(
            "local lie service: the CT card contains non-ASCII characters and was "
            "ignored (masked: %s)", _mask(raw),
        )
        return ""
    for token in raw.replace(",", " ").replace(";", " ").split():
        candidate = token.strip().upper()
        if candidate.startswith("CT_") and CARD_PATTERN.match(candidate):
            if candidate != raw.upper():
                LOG.warning(
                    "local lie service: the delivered CT card was not clean "
                    "(len %d, %d token(s)); using %s",
                    len(raw), len(raw.split()), _mask(candidate),
                )
            return candidate
    return ""


def _live_endpoint_host() -> str:
    """The deployment host from the compiled endpoint table, or ""."""

    try:
        import endpoints_protected
    except Exception:  # noqa: BLE001 - the table is optional for this helper
        return ""
    try:
        hosts = endpoints_protected.endpoint_hosts()
    except Exception:  # noqa: BLE001
        return ""
    return str(hosts[0]) if hosts else ""


def _auth_server_url(current: str = "") -> str:
    """The CT card authorization URL, always on ``AUTH_SERVER_PORT``.

    The configured host wins; only when it is empty - or names a retired
    deployment - is the live host from the endpoint table used.
    """

    raw = str(current or "").strip()
    scheme, host = "http", ""
    if raw:
        if "://" in raw:
            scheme, rest = raw.split("://", 1)
            scheme = scheme or "http"
        else:
            rest = raw
        rest = rest.split("/", 1)[0]
        host = rest.rpartition(":")[0] if ":" in rest else rest
    host = host.strip()
    if host == "222.215.232.230":
        host = ""                      # retired deployment
    if not host:
        host = _live_endpoint_host()
    if not host:
        return raw                     # nothing to build from: leave it alone
    return f"{scheme}://{host}:{AUTH_SERVER_PORT}"


def normalize_auth_server(app_root: Path | str) -> bool:
    """Force the card authorization port on both service config files.

    ``data/ct_login_config.json`` is what the service verifies the card with; the
    package-root ``login_config.json`` is what the engine shim reads.  A file is
    only rewritten when its URL actually changes, and every other key it carries
    is preserved.
    """

    changed = False
    targets: list[Path] = []
    credentials = service_credentials_path(app_root)
    if credentials is not None and credentials.is_file():
        targets.append(credentials)
    root = find_service_root(app_root)
    if root is not None:
        legacy = root / AUTH_SERVER_FILE
        if legacy.is_file():
            targets.append(legacy)
    for target in targets:
        settings = _read_service_settings(target)
        if not isinstance(settings, dict):
            continue
        current = str(settings.get("license_server_url") or "")
        wanted = _auth_server_url(current)
        if not wanted or wanted == current:
            continue
        settings["license_server_url"] = wanted
        try:
            _write_service_settings(target, settings)
        except OSError as exc:
            LOG.warning("local lie service: could not pin %s (%s)", target.name, exc)
            continue
        LOG.warning(
            "local lie service: card authorization URL pinned to %s in %s (was %s)",
            wanted, target.name, current or "(empty)",
        )
        changed = True
    return changed


def write_credentials(
    app_root: Path | str, card_code: str, product_key: str = "",
) -> Path:
    """Write the CT_IN card into the service config (and the live WS key).

    本地测谎 needs the CT_* card in ``data/ct_login_config.json``.  The coordinate
    cloud key is a DIFFERENT credential: the field ``lie_product_key`` is what the
    service handshakes ``ws://<host>:8888`` with.

    Measured against the real tracking server (2026-11-16): the key the
    heartbeat delivers (``tw-x87…4323``) is accepted with ``auth=True
    quota_left=100``, while the constant this package ships (``LIE-PLANET-001``)
    is refused with ``unknown_or_deleted_product_key``.  So the delivered key
    REPLACES the package's own constant and any placeholder/empty field; any
    other real key is kept (it may be the partner-issued one).  The document keeps
    every other setting it was shipped with, including the ones we never read
    (``vm_pierce_*``, ``lie3_ort_ep``, the http/ws fallback flags).  It is written
    as ASCII-only JSON without a BOM: the 2026-11 package ships this file in GBK,
    so escaped Unicode is the only form both readers agree on.
    """

    path = service_credentials_path(app_root)
    if path is None:
        raise LocalLieServiceError("未找到本地测谎服务目录，无法写入激活信息。")
    card = canonical_card(card_code) or str(card_code or "").strip()
    if not card.upper().startswith("CT_"):
        raise LocalLieServiceError("激活码无效：请输入 CT_ 开头的测谎卡。")
    settings = _load_service_settings(path, what="the service credentials")
    if settings is None:
        # Never replace a document that could not be read: that is how our write
        # used to wipe the vendor's license_server_url / backend / vm switches.
        raise LocalLieServiceError(
            "本地测谎服务配置无法解析，为避免覆盖其原有设置，本次没有写入激活信息。"
        )
    settings["partner_license_code"] = card
    # The card can only validate on the documented authorization port.
    auth_url = _auth_server_url(str(settings.get("license_server_url") or ""))
    if auth_url and auth_url != str(settings.get("license_server_url") or ""):
        LOG.warning(
            "local lie service: card authorization URL corrected to %s", auth_url,
        )
        settings["license_server_url"] = auth_url
    key = normalise_credential(product_key)
    if key and _product_key_writable(app_root, settings):
        if credential_has_cjk(key):
            LOG.warning(
                "local lie service: the delivered WS key contains non-ASCII "
                "characters and was not written (masked: %s)", _mask(key),
            )
        else:
            settings["lie_product_key"] = key
            LOG.info(
                "local lie service: WS product key taken from the activation server "
                "(card=%s, key=%s)",
                _mask(card), _mask(key),
            )
    elif key:
        LOG.info(
            "local lie service: keeping the configured WS product key "
            "(the activation-server key replaces it only when the field is empty, a "
            "placeholder, or the constant this package ships)"
        )
    # The constant this package ships is NEVER written (see
    # normalize_placeholder_ws_key): the tracking server answers
    # unknown_or_deleted_product_key for it, so writing it would turn a
    # not-yet-configured service into a permanently failing one.  An empty field
    # simply waits for the licence heartbeat's key.
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_service_settings(path, settings)
    _sync_partner_card_state(find_service_root(app_root), card)
    LOG.info(
        "local lie service: credentials written to %s (card=%s, ws key=%s)",
        path, _mask(card), _mask(str(settings.get("lie_product_key") or "")) or "none",
    )
    return path


def _sync_partner_card_state(root: Optional[Path], card: str) -> None:
    """Keep the vendor's remembered code (and its ticket) in step with *card*.

    The service keeps ``data/license/ct_partner_last_code.json`` - the code it
    activated last - and the ticket that code received in
    ``ct_partner_in_state.json``.  Both OUTRANK ``ct_login_config.json``: a start
    validates the remembered code (and its cached ticket) even when the config
    carries a different card, so a card change written only into the config never
    took effect.  Proved on 2026-11-15: the operator's test card was written to
    the config and passed as ``--license-code``, yet the vendor validated the
    previous card because of these two files.

    A ticket issued for another code cannot validate this one, so it is removed
    (the vendor re-activates the card it was given) and the remembered code is
    rewritten.  Only files that already exist are touched: a fresh package has
    neither.
    """

    if root is None or not card:
        return
    state_dir = Path(root) / "data" / "license"
    if not state_dir.is_dir():
        return
    last_code = state_dir / "ct_partner_last_code.json"
    ticket = state_dir / "ct_partner_in_state.json"
    recorded = ""
    if last_code.is_file():
        try:
            recorded = str(
                (json.loads(last_code.read_text(encoding="utf-8", errors="replace")) or {})
                .get("license_code") or ""
            ).strip()
        except (OSError, ValueError):
            recorded = ""
        if recorded != card:
            try:
                last_code.write_text(
                    json.dumps({"license_code": card}, ensure_ascii=True) + "\n",
                    encoding="utf-8",
                )
                LOG.info(
                    "local lie service: the service's remembered CT card now follows "
                    "the config (%s)", _mask(card),
                )
            except OSError as exc:
                LOG.warning(
                    "local lie service: could not update the remembered CT card (%s)",
                    exc,
                )
    if not ticket.is_file():
        return
    try:
        state = json.loads(ticket.read_text(encoding="utf-8", errors="replace")) or {}
    except (OSError, ValueError):
        state = {}
    ticket_card = str(state.get("license_code") or "").strip()
    if not ticket_card or ticket_card == card:
        return
    try:
        ticket.unlink()
        LOG.warning(
            "local lie service: the cached CT ticket belonged to %s; removed so the "
            "new card %s is activated", _mask(ticket_card), _mask(card),
        )
    except OSError as exc:
        LOG.warning("local lie service: could not remove the stale CT ticket (%s)", exc)


def local_binding_files(app_root: Path | str) -> tuple[Path, ...]:
    """The per-device licence state the service writes after activating."""

    root = find_service_root(app_root)
    if root is None:
        return ()
    state = root / "data" / "license"
    return (state / "ct_partner_in_state.json", state / "ct_partner_last_code.json")


def _recorded_card(app_root: Path | str) -> str:
    """The card this install last activated with, or ""."""

    for path in local_binding_files(app_root)[1:]:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = str(data.get("license_code") or "").strip()
        if value:
            return value
    return ""


def clear_local_binding(app_root: Path | str) -> int:
    """Drop the local ticket + last code, so the next start really re-activates.

    The service reads its ticket from ``data/license``.  A device that already
    holds a ticket for ANOTHER card would otherwise keep using it, and the
    operator's rule is: pressing 激活测谎 with a key makes THIS device take that
    key over.  Removing the state forces the service's own ``activate`` call.
    """

    removed = 0
    for path in local_binding_files(app_root):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError as exc:
            LOG.warning("local lie service: could not remove %s (%s)", path, exc)
    if removed:
        LOG.info("local lie service: local licence binding cleared (%d file(s))", removed)
    return removed


def install_root() -> Path:
    """The assistant's install root (works for source AND frozen builds).

    ``__file__`` is reliable in the source tree, but inside a Nuitka build the
    module path is not the program folder, so the canonical helper is used first.
    A wrong root here means "the packaged service folder was not found" and the
    service silently never starts - the field symptom "已启动 by hand only".
    """

    try:
        from runtime_paths import application_root

        return Path(application_root())
    except Exception:  # noqa: BLE001 - never break a start attempt
        return Path(__file__).resolve().parent


# Optional probe the UI installs: True while a lie pass is running, so a key
# refresh never restarts the service underneath a live pass.
_busy_probe: Optional[Callable[[], bool]] = None


def set_busy_probe(probe: Optional[Any]) -> None:
    """Install ``probe() -> bool`` (True = a pass owns the service right now)."""

    global _busy_probe
    _busy_probe = probe if callable(probe) else None


def _busy() -> bool:
    probe = _busy_probe
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a probe must never break a refresh
        return False


def sync_product_key(product_key: str, app_root: Path | str | None = None) -> bool:
    """Write a licence-server WS key into the local service config.

    The activation server's reply is the authority for the coordinate-cloud key -
    the same secret the remote 测谎 backend receives - so the local service gets
    it here instead of keeping only the key baked into its package.  Returns True
    when the file changed; a service this app started is then restarted so the
    new key is really used (never while a lie pass is running).
    """

    key = str(product_key or "").strip()
    if not key:
        return False
    root = Path(app_root) if app_root is not None else install_root()
    path = service_credentials_path(root)
    if path is None or not path.is_file():
        return False
    settings = _load_service_settings(path, what="the service credentials")
    if settings is None:
        return False
    if normalise_credential(settings.get("lie_product_key")) == key:
        return False
    if not _product_key_writable(root, settings):
        # The configured key is a real one that is not the packaged constant:
        # keep it (it may be the partner-issued key).  The shipped constant is
        # never written here either - the tracking server refuses it.
        LOG.info(
            "local lie service: WS key refresh skipped - the config already carries "
            "a real product key (%s), and the activation-server key never replaces "
            "one", _mask(normalise_credential(settings.get("lie_product_key")))
        )
        return False
    settings["lie_product_key"] = key
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the WS key could not be written (%s)", exc)
        return False
    LOG.info(
        "local lie service: WS product key refreshed from the activation server "
        "reply (key=%s)",
        _mask(key),
    )
    with _lock:
        started_by_app = bool(_state.get("started_by_app"))
    if started_by_app and not _busy():
        # Hand the restart to the supervisor instead of running ``ensure_started``
        # inline: this function is called from the licence-reply handler, and a
        # cold start waits up to START_WAIT_SECONDS (90 s) - during which the
        # header would still read 正在验证在线授权 while the Tk thread was blocked in
        # ``hint_state`` on the same lock (field report: the restarted instance
        # froze there).  The card path below has always been non-blocking; this
        # one now matches it.
        LOG.info("local lie service: restarting so the refreshed key takes effect")
        stop_started_service()
        reset_for_tests()
        supervisor_set_active(True, root)
    return True


def apply_online_card(card_code: str) -> bool:
    """Use the CT card the licence server delivered (no manual entry).

    The operator's rule: the activation code now carries a ``ct_card``, the server
    returns it in the licence document, and the desktop starts the local service
    with it - the 激活测谎 button is gone.  The card is written only when it
    differs from what this install already uses; a running service this app
    started is then restarted (never under a live pass).
    """

    card = canonical_card(card_code) or str(card_code or "").strip()
    if not card:
        return False
    # The card is known from the licence reply whether or not the CT package can
    # be written right now, so the access decision (grey 本地测谎 or not) is taken
    # from the reply itself, before any file work.
    with _lock:
        _state["online_card"] = card
        _state["card_checked"] = True
    # ARMS THE START GATE, and it must happen before every early ``return False``
    # below.  When the vendor config already carries this very card, the write
    # below is a no-op and this function used to return without ever telling the
    # supervisor that the reply had arrived - so the panel sat on 等待授权信息 for
    # ever and the service only started when the radio was toggled (16:19 field
    # log: "i have to switch to 云端测谎 and switch back").
    note_online_card(card)
    root = install_root()
    path = service_credentials_path(root)
    if path is None or not path.is_file():
        LOG.info(
            "local lie service: the CT card is known from the licence reply, but the "
            "service credentials file is missing (%s); the service starts with the "
            "card already in its config", path,
        )
        return False
    settings = _load_service_settings(path, what="the service credentials")
    if settings is None:
        # The card is still known from the reply (the access decision above), but
        # an unreadable document is left alone instead of being replaced.
        return False
    changed = False
    if normalise_credential(settings.get("partner_license_code")) != card:
        settings["partner_license_code"] = card
        changed = True
    # The WS product key is NOT touched here: ``normalize_placeholder_ws_key``
    # (called on the way into every start) is the one owner of that field, and it
    # writes only a key the tracking server accepts.  Writing this package's own
    # constant here is what produced the field state
    # ``"partner_license_code": "CT_IN-…", "lie_product_key": "LIE-PLANET-001"``,
    # which the server refuses with unknown_or_deleted_product_key.
    if not changed:
        # Nothing to rewrite: the file this install already uses matches the reply,
        # and ``note_online_card`` above has already armed the start.
        return False
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the online card could not be written (%s)", exc)
        return False
    LOG.info(
        "local lie service: CT card from the activation server written to %s (%s)",
        path, _mask(card),
    )
    # The card on disk changed, so a service this app started must pick it up.
    # The restart is handed to the supervisor (armed above) instead of blocking
    # this heartbeat thread on a 90 s ``ensure_started``.
    with _lock:
        started_by_app = bool(_state.get("started_by_app"))
    if started_by_app and not _busy():
        LOG.info("local lie service: restarting so the online card takes effect")
        stop_started_service()
        reset_for_tests()
        supervisor_set_active(True, root)
    return True


def report_local_pass(
    event_id: str, outcome: str, *, frames: int = 0,
    latency_ms: Optional[float] = None, detail: str = "",
    on_result: Optional[Callable[[dict[str, Any]], None]] = None,
) -> None:
    """Report one 本地测谎 pass to the activation server (background, best effort).

    Local passes are billed by the CT service, so this only feeds the server's
    statistics table (``api/v1/local-lie-events``).  It never blocks a pass and
    never raises: a lost report must not disturb the lie window.
    """

    def job() -> None:
        try:
            import licensing

            answer = licensing.post_json(
                licensing.ACTIVATION_SERVER_ENDPOINT,
                "/api/v1/local-lie-events",
                {
                    "fingerprint": licensing.machine_fingerprint_hash(),
                    "license_id": licensing.verify_license().license_id or "",
                    "event_id": str(event_id),
                    "outcome": str(outcome),
                    "frames": int(frames or 0),
                    "latency_ms": latency_ms,
                    "detail": str(detail or "")[:200],
                    "ct_card": str(_state.get("online_card") or "")[:30],
                    # An explicit UTC offset: the server stores this instant, and a
                    # naive string made an un-updated server answer 500 (Django 5
                    # dropped django.utils.timezone.utc on that branch), which the
                    # field log showed as "category=JSONDecodeError" on this path.
                    "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                licensing.runtime_root() / licensing.ACTIVATION_SERVER_PIN_FILE,
            )
            if isinstance(answer, dict) and callable(on_result):
                # This runs on the report thread.  The UI callback only queues
                # the answer; it must never manipulate Tk from this worker.
                try:
                    on_result(answer)
                except Exception:  # noqa: BLE001 - statistics stay best effort
                    LOG.debug("local lie service: result callback failed", exc_info=True)
            if isinstance(answer, dict) and answer.get("ok") is False:
                # A rejected report is not an exception, so it would otherwise be
                # invisible: say why, without ever echoing the card.
                LOG.warning(
                    "local lie service: the server did not accept pass %s (%s): %s",
                    str(event_id)[:8], outcome, str(answer.get("message") or "")[:120],
                )
                return answer
            LOG.info("local lie service: pass %s reported (%s)", str(event_id)[:8], outcome)
            return answer
        except Exception:  # noqa: BLE001 - statistics are never critical
            LOG.warning("local lie service: the pass report failed", exc_info=True)
            return None

    threading.Thread(target=job, name="local-lie-report", daemon=True).start()


def activate(
    app_root: Path | str, card_code: str, product_key: str = "",
    *, wait_seconds: float = START_WAIT_SECONDS,
) -> tuple[bool, str]:
    """Write the card, restart the service and confirm the licence validated.

    This is the whole 激活测谎 flow: the service validates the CT_IN card against
    the licence server on startup, so a restart IS the activation - and a fresh
    activation takes the card's binding over to this device (the vendor's
    "后激活抢绑").  No unbind step is required.  When the card differs from the
    one this install last used, the local ticket is cleared first so the service
    performs a real activate call instead of reusing the old ticket.
    Returns ``(ok, note)`` where the note is panel-ready and never contains the
    card.
    """

    try:
        write_credentials(app_root, card_code, product_key)
    except LocalLieServiceError as exc:
        note = str(exc)
        with _lock:
            _state.update(state="failed", note=note)
        return False, note
    new_card = str(card_code or "").strip()
    previous = _recorded_card(app_root)
    if previous and previous.casefold() != new_card.casefold():
        LOG.info(
            "local lie service: the card changed (%s -> %s); clearing the local "
            "ticket so this device takes the new card over",
            _mask(previous), _mask(new_card),
        )
        clear_local_binding(app_root)
    stop_started_service()
    reset_for_tests()
    ok, note = ensure_started(app_root, wait_seconds=wait_seconds)
    if not ok:
        return False, note
    running, detail = _probe()
    if not running:
        return False, detail
    with _lock:
        _state.update(state="running", note=detail)
    return True, detail


def hint_state() -> str:
    """Which hint_text() branch applies right now.

    ``off`` / ``no_card`` / ``waiting`` / ``verifying`` / ``starting`` / ``up`` /
    ``failed``.
    ``waiting`` is the operator's 等待授权信息 stage: 本地测谎 is selected and a CT
    card exists somewhere, but no *validated reply of this session* has delivered
    it yet, so nothing may be started (2026-11-13 rule: wait for the heartbeat,
    start on its card).
    """

    state_snapshot = read_state()
    state = str(state_snapshot.get("state") or "idle")
    rejected = bool(state_snapshot.get("card_rejected"))
    desired = _desired_active
    denied = bool(state_snapshot.get("card_denied"))
    if not desired:
        return "off"
    if state in ("running", "external"):
        # A service that answers is up.  The latch explains a service that will
        # NOT come up; letting it win here is what kept the panel at 启动失败 while
        # the service was serving passes.
        return "up"
    if denied or not has_online_card():
        return "no_card"
    if rejected:
        return "failed"
    if state == "verifying":
        # The credentials are being checked before a launch is even attempted (a
        # launch without a usable WS key would be refused by the tracking server).
        return "verifying"
    if state == "starting":
        return "starting"
    if state == "waiting":
        # Once a validated reply has delivered the card the waiting stage is over,
        # even if the launch has not begun yet: the panel must not keep saying
        # 等待授权信息 while the authorisation is already in hand (16:19 field log).
        return "waiting" if not _card_from_reply() else "starting"
    if state == "missing":
        # No service folder beside this build: that is not "waiting for the
        # licence", nothing will ever arrive to start it.
        return "failed"
    if state == "idle":
        return "waiting" if not _card_from_reply() else "off"
    return "failed"

def hint_text() -> str:
    """One short sentence for the 图层校准与巡逻 hint line, or "".

    The operator's set is exactly these service sentences, and none of them
    carries a reason, a retry cadence or any other developer note — this build
    ships to clients:
      * 本地测谎：启动失败    - the service refused, died, or gave up
      * 本地测谎: 等待授权信息 - selected, waiting for a validated licence
        reply to deliver the CT card
      * 本地测谎服务：正在校验，还未启动 - the card/key are being verified; no
        launch has been attempted yet
      * 本地测谎服务：正在启动，还未成功 - a launch is in flight (the operator's
        wording: it must not read like a state that already works)
      * 本地测谎: 启动成功     - /v1/health answers
    Plus 本地测谎未开通 when this activation code carries no CT card at all (that is
    an access fact, not a service state: the radio is labelled 本地测谎（未开通）),
    and nothing at all for 关闭 / 云端测谎.
    """

    state = hint_state()
    if state == "off":
        return ""
    if state == "no_card":
        return "本地测谎未开通"
    if state == "up":
        return "本地测谎: 启动成功"
    if state == "verifying":
        return "本地测谎服务：正在校验，还未启动"
    if state == "starting":
        return "本地测谎服务：正在启动，还未成功"
    if state == "waiting":
        return "本地测谎: 等待授权信息"
    # ONE word for the client: 启动失败.  The vendor's own reason ("坐标云启动握手失败")
    # is a developer sentence a player can neither act on nor understand, so it
    # stays in assistant.log (`_log_vendor_reason` / `note_failure` write it on
    # every failure) and never reaches the panel hint.  No cadence/retry note
    # either: the supervisor keeps retrying every SLOW_RETRY_SECONDS without
    # asking, which is not something the operator has to read.
    return "本地测谎：启动失败"

def reset_for_tests() -> None:
    global _launch_process, _start_attempts, _selected_at, _heartbeat_wait_logged
    global _card_wait_thread, _last_attempt_booting
    with _lock:
        _state.update(state="idle", note="", root="", started_by_app=False, pids=[],
                      card_rejected=False, card_denied=False)
        # The launcher handle belongs to the start attempt, so a reset must forget
        # it as well: otherwise a later start sees "a launcher is still booting"
        # and refuses to start the service at all.
        _launch_process = None
        # The start budget and the heartbeat wait are per-selection state.
        _start_attempts = 0
        _selected_at = 0.0
        _card_wait_thread = None
        _last_attempt_booting = False
    _heartbeat_wait_logged = False


__all__ = [
    "SERVICE_FOLDER", "LocalLieServiceError", "activate", "ensure_started",
    "failure_reason", "find_service_root", "hint_text", "is_running", "note_failure",
    "reset_for_tests", "service_credentials_path", "start_in_background",
    "stop_started_service", "sync_product_key", "set_busy_probe", "install_root", "write_credentials",
]
