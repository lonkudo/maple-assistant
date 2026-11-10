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
import signal
import subprocess
import threading
import time
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
START_WAIT_SECONDS = 45.0
PROBE_INTERVAL_SECONDS = 0.5
# Pre-start supervision: how often the supervisor re-checks that 8765 answers.
SUPERVISE_INTERVAL_SECONDS = 10.0
# Startup supervision: a first launch can exit while the licence/WS are being
# provisioned, so a service that is not listening is retried after this delay.
RETRY_BACKOFF_SECONDS = 20.0

# Re-entrant on purpose: several helpers take it (``has_online_card`` and
# ``card_known`` read the state) and the failure paths of ``ensure_started`` call
# them from inside their own ``with _lock`` block.  With a plain Lock that nesting
# deadlocked the calling thread **while holding the lock**, which froze every later
# probe, hint and supervisor iteration - the field symptom "本地测谎 一直显示正在
# 启动/没有任何反应" after one start that could not come up (no CT card, licence
# refused, or a slow cold start).
_lock = threading.RLock()
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


class LocalLieServiceError(RuntimeError):
    """The local service cannot be used or activated; the text is panel-ready."""


def find_service_root(app_root: Path | str) -> Optional[Path]:
    """The folder holding the packaged CT partner service, or None."""

    root = Path(app_root)
    candidates = [
        root / SERVICE_FOLDER,
        root / SERVICE_FOLDER.parent,
        root,
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


def _service_pids(root: Path) -> list[int]:
    """PIDs whose executable lives inside ``root`` (the service binaries)."""

    pids: list[int] = []
    try:
        import win32api  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]

        wanted = str(root.resolve()).casefold()
        for pid in win32process.EnumProcesses():
            if pid in (0, 4):
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
            if path.casefold().startswith(wanted) and path.casefold().endswith(".exe"):
                pids.append(int(pid))
    except Exception:  # noqa: BLE001 - pywin32 missing: fall back to names
        LOG.debug("local lie service: pid discovery unavailable", exc_info=True)
        for name in _binary_names(root):
            for pid in _pids_by_name(name):
                if pid not in pids:
                    pids.append(pid)
    return sorted(set(pids))


def _build_profile_exe_stem(root: Path) -> str:
    """The service binary name the package itself declares, or ""."""

    profile = _read_service_settings(Path(root) / "data" / "build_profile.json")
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("client_exe_stem") or "").strip()


def _binary_names(root: Path) -> list[str]:
    """Executables in the service folder, the package's declared one first."""

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
        if name not in ordered:
            ordered.append(name)
    return ordered


def _pids_by_name(image: str) -> list[int]:
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


def _launch(root: Path) -> Optional[subprocess.Popen]:
    """Start the packaged service hidden (WSH window style 0)."""

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


def ensure_started(
    app_root: Path | str, *, wait_seconds: float = START_WAIT_SECONDS,
) -> tuple[bool, str]:
    """Start the service when it is not already listening; never raises."""

    root = find_service_root(app_root)
    with _lock:
        if root is None:
            _state.update(state="missing", note="未找到本地测谎服务目录",
                          root="", started_by_app=False, pids=[])
            LOG.info("local lie service: the package folder is not installed "
                     "next to this build, so 本地测谎 stays external")
            return False, str(_state["note"])
        running, note = _probe()
        if running:
            _state.update(state="external", note=note, root=str(root),
                          started_by_app=False, pids=[])
            LOG.info("local lie service: already running (%s)", note)
            return True, note
        _state.update(state="starting", note="正在启动本地测谎服务…",
                      root=str(root), started_by_app=True, pids=[])
    LOG.info("local lie service: starting %s", root)
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
    try:
        with _lock:
            memory_card = str(_state.get("online_card") or "").strip()
        with _lock:
            denied = bool(_state.get("card_denied"))
        if memory_card and not denied and config_card(root) != memory_card:
            write_credentials(root, memory_card)
            LOG.info(
                "local lie service: the service config had lost its CT card (%s); "
                "rewrote it before starting",
                _mask(memory_card),
            )
    except LocalLieServiceError as exc:
        LOG.warning("local lie service: the CT card could not be restored (%s)", exc)
    except Exception:  # noqa: BLE001 - a repair must never block the start
        LOG.debug("local lie service: CT card restore failed", exc_info=True)
    process = _launch(root)
    if process is None:
        rejected = has_online_card()      # read outside the lock (see _lock)
        with _lock:
            _state.update(state="failed", note="本地测谎服务无法启动（缺少启动器）")
            if rejected:
                # A card WAS delivered, yet the service cannot come up: on this
                # machine that means the card did not validate.
                _state["card_rejected"] = True
        return False, str(_state["note"])
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        time.sleep(PROBE_INTERVAL_SECONDS)
        running, note = _probe()
        if running:
            pids = _service_pids(root)
            with _lock:
                _state.update(state="running", note=note, pids=pids,
                              started_by_app=True, card_rejected=False)
            LOG.info("local lie service: started (pid=%s, %s)", pids, note)
            return True, note
    # The licence server gave us a CT card and the service still refuses to listen:
    # its own log shows 启动月卡验票失败 for an unactivated card.
    rejected = has_online_card()          # read outside the lock (see _lock)
    with _lock:
        _state.update(state="failed", note="本地测谎服务启动超时（8765 未响应）")
        if rejected:
            _state["card_rejected"] = True
    LOG.warning("local lie service: still not answering after %.0fs",
                wait_seconds)
    return False, str(_state["note"])


_supervisor_thread = None
_supervisor_stop = threading.Event()
# The selection wants 本地测谎 (_desired_active), the install root the supervisor
# uses (_desired_root), and the CT card the licence server delivered
# (_state["online_card"]).  The service is started only when the selection wants it
# AND a CT_ card exists - it is never started blindly at launch.
_desired_active = False
_desired_root: Optional[Path] = None


def _supervisor_job(root, interval: float) -> None:
    first = True
    while not _supervisor_stop.is_set():
        try:
            if not is_running():
                if first:
                    LOG.info("local lie service: pre-starting with the app (%s)", root)
                else:
                    LOG.info("local lie service: 8765 stopped answering; "
                             "pre-starting it again")
                ensure_started(root)
        except Exception:  # noqa: BLE001 - supervision never dies
            LOG.warning("local lie service: supervisor start failed", exc_info=True)
        first = False
        _supervisor_stop.wait(interval)


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
    card = str(settings.get("partner_license_code") or "").strip()
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

    with _lock:
        if _state.get("card_denied"):
            return ""
        card = str(_state.get("online_card") or "").strip()
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

    with _lock:
        decided = bool(_state.get("card_checked"))
    return decided or bool(config_card())


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

    global _supervisor_thread, _desired_active, _desired_root
    if not active:
        _supervisor_stop.set()
        with _lock:
            _supervisor_thread = None
            _desired_active = False
            _state["card_rejected"] = False
        stop_started_service()
        LOG.info("local lie service: pre-start disabled (selection left 本地测谎)")
        return
    root = Path(app_root) if app_root is not None else install_root()
    with _lock:
        _desired_active = True
        _desired_root = root
        running = _supervisor_thread is not None and _supervisor_thread.is_alive()
    if not has_online_card():
        LOG.info(
            "local lie service: 本地测谎 selected, but the licence server has not "
            "delivered a CT card yet - the service is not started until it does"
        )
        return
    if running:
        return
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
    """Record the CT card from the licence reply and pre-start if it is wanted."""

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
    if not text:
        return
    if desired:
        LOG.info("local lie service: the CT card arrived; pre-starting the service")
        _start_supervisor_thread(root or install_root())


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
    """Stop the service IF this app started it; returns how many were stopped."""

    with _lock:
        started = bool(_state.get("started_by_app"))
        root_text = str(_state.get("root") or "")
        known = [int(pid) for pid in (_state.get("pids") or [])]
    if not started or not root_text:
        LOG.info("local lie service: left running (this app did not start it)")
        return 0
    root = Path(root_text)
    pids = known or _service_pids(root)
    stopped = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except OSError:
            LOG.debug("local lie service: pid %s already gone", pid)
    if not pids:
        for image in _binary_names(root):
            for pid in _pids_by_name(image):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    stopped += 1
                except (OSError, subprocess.SubprocessError):
                    continue
    with _lock:
        _state.update(state="idle", note="本地测谎服务已随程序停止",
                      started_by_app=False, pids=[])
    LOG.info("local lie service: stopped %d process(es) with the app", stopped)
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


def _looks_like_placeholder(value: object) -> bool:
    """Whether a config value is still the vendor's template text."""

    text = str(value or "").strip()
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
    """A usable WS key for the file: the real one it has, else the package's own.

    The updated package ships the *placeholder* ``LIE-你的坐标云密钥`` where a key
    belongs.  Sending that to the coordinate cloud fails the startup handshake
    (``handshake_failed``) and 8765 never listens, so a placeholder is replaced
    with the package's own ``build_profile.json`` key - the same key the service
    would have used had the field been empty.
    """

    current = str(settings.get("lie_product_key") or "").strip()
    if not _looks_like_placeholder(current):
        return current
    return _build_profile_ws_key(app_root)


def normalize_placeholder_ws_key(app_root: Path | str) -> bool:
    """Replace a template WS key with the package's own, before the service starts.

    Called on the way into every start: the operator may put the CT_IN code in
    ``data/ct_login_config.json`` by hand (the vendor's documented way) without a
    licence reply ever arriving, and the shipped placeholder key would then fail
    the startup handshake.  Returns True when the file changed.
    """

    path = service_credentials_path(app_root)
    if path is None or not path.is_file():
        return False
    settings = _read_service_settings(path)
    if settings is None:
        return False
    current = str(settings.get("lie_product_key") or "").strip()
    if not _looks_like_placeholder(current):
        return False
    replacement = _build_profile_ws_key(app_root)
    if not replacement:
        return False          # nothing better to write: leave the file alone
    settings["lie_product_key"] = replacement
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the WS key could not be written (%s)", exc)
        return False
    LOG.info(
        "local lie service: the placeholder WS key in %s was replaced by the "
        "package's own key (%s) so the startup handshake can pass",
        path.name, _mask(replacement),
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
    """Whether a licence-server key may replace the local WS key.

    The operator's rule for testing the local service: **the package's own key is
    used**.  The activation-server key is only a fallback for a build that ships
    none (or whose config already carries one of ours), because the two keys
    belong to different endpoints (the package key handshakes the service's own
    ``:8888``; the heartbeat key is issued for the ``8001-8003`` cloud).
    """

    if _looks_like_placeholder(settings.get("lie_product_key")):
        # Nothing usable yet: ours may fill it only when the package ships none.
        return not _build_profile_ws_key(app_root)
    own = _build_profile_ws_key(app_root)
    if own and str(settings.get("lie_product_key") or "").strip().casefold() == own.casefold():
        # The vendor's OWN key for its own endpoint.  The handshake of the 2026-11
        # build succeeds with it (measured: ws_startup_handshake ok, quota 6172), so
        # it is never replaced by the activation server's cloud key.
        return False
    return True                          # ours already: keep it refreshed


def write_credentials(
    app_root: Path | str, card_code: str, product_key: str = "",
) -> Path:
    """Write the CT_IN card into the service config (the WS key only as fallback).

    本地测谎 needs the CT_* card in ``data/ct_login_config.json``.  The coordinate
    cloud key is a DIFFERENT thing here: the service package ships its own
    ``ws_product_key`` in ``data/build_profile.json`` and handshakes its own
    endpoint with it (``ws://<host>:8888``), while the licence heartbeat's key
    belongs to the ``8001-8003`` cloud the REMOTE 测谎 backend uses.  Overwriting
    a working key would point the service at the wrong endpoint, so the caller's
    key is written ONLY when the config field is empty (or still the vendor's
    placeholder) and the package carries none of its own.  The document keeps every
    other setting it was shipped with, including the ones we never read
    (``vm_pierce_*``, ``lie3_ort_ep``, the http/ws fallback flags).  It is written
    as ASCII-only JSON without a BOM: the 2026-11 package ships this file in GBK,
    so escaped Unicode is the only form both readers agree on.

    A placeholder ``lie_product_key`` is replaced with the package's own
    ``build_profile.json`` key, because the literal template text would fail the
    startup WS handshake and 8765 would never listen.
    """

    path = service_credentials_path(app_root)
    if path is None:
        raise LocalLieServiceError("未找到本地测谎服务目录，无法写入激活信息。")
    card = str(card_code or "").strip()
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
    key = str(product_key or "").strip()
    if key and _product_key_writable(app_root, settings):
        settings["lie_product_key"] = key
        LOG.info(
            "local lie service: WS product key taken from the activation server "
            "(card=%s, key=%s)",
            _mask(card), _mask(key),
        )
    else:
        own = _package_ws_key_or_empty(app_root, settings)
        if own and str(settings.get("lie_product_key") or "").strip() != own:
            settings["lie_product_key"] = own
            LOG.info(
                "local lie service: the placeholder WS key was replaced by the "
                "package's own key (%s)", _mask(own),
            )
        if key:
            LOG.info(
                "local lie service: keeping the package's own WS product key for now "
                "(the activation-server key is only used by a package that ships none)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_service_settings(path, settings)
    LOG.info(
        "local lie service: credentials written to %s (card=%s, ws key=%s)",
        path, _mask(card), _mask(str(settings.get("lie_product_key") or "")) or "none",
    )
    return path


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
    placeholder_fix = _package_ws_key_or_empty(root, settings)
    if str(settings.get("lie_product_key") or "").strip() == key:
        return False
    if not _product_key_writable(root, settings):
        # Still clean the vendor's template key up, so the service can handshake
        # with the package's own key instead of the placeholder text.
        if (placeholder_fix
                and str(settings.get("lie_product_key") or "").strip() != placeholder_fix):
            settings["lie_product_key"] = placeholder_fix
            try:
                _write_service_settings(path, settings)
            except OSError as exc:
                LOG.warning("local lie service: the WS key could not be written (%s)", exc)
                return False
            LOG.info(
                "local lie service: placeholder WS key replaced by the package's own "
                "key (%s); the activation-server key was not applied",
                _mask(placeholder_fix),
            )
            return True
        LOG.info(
            "local lie service: WS key refresh skipped - the package's own key is "
            "in use (as requested while testing the local service)"
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
        LOG.info("local lie service: restarting so the refreshed key takes effect")
        stop_started_service()
        reset_for_tests()
        ensure_started(root)
    return True


def apply_online_card(card_code: str) -> bool:
    """Use the CT card the licence server delivered (no manual entry).

    The operator's rule: the activation code now carries a ``ct_card``, the server
    returns it in the licence document, and the desktop starts the local service
    with it - the 激活测谎 button is gone.  The card is written only when it
    differs from what this install already uses; a running service this app
    started is then restarted (never under a live pass).
    """

    card = str(card_code or "").strip()
    if not card:
        return False
    # The card is known from the licence reply whether or not the CT package can
    # be written right now, so the access decision (grey 本地测谎 or not) is taken
    # from the reply itself, before any file work.
    with _lock:
        _state["online_card"] = card
        _state["card_checked"] = True
    root = install_root()
    path = service_credentials_path(root)
    if path is None or not path.is_file():
        return False
    settings = _load_service_settings(path, what="the service credentials")
    if settings is None:
        # The card is still known from the reply (the access decision above), but
        # an unreadable document is left alone instead of being replaced.
        return False
    changed = False
    if str(settings.get("partner_license_code") or "").strip() != card:
        settings["partner_license_code"] = card
        changed = True
    # The updated package ships the WS key as a placeholder; replacing it with the
    # package's own key is what lets the startup handshake succeed.
    own = _package_ws_key_or_empty(root, settings)
    if own and str(settings.get("lie_product_key") or "").strip() != own:
        settings["lie_product_key"] = own
        changed = True
        LOG.info(
            "local lie service: placeholder WS key replaced by the package's own "
            "key (%s)", _mask(own),
        )
    if not changed:
        with _lock:
            _state["online_card"] = card
            _state["card_checked"] = True
        return False
    try:
        _write_service_settings(path, settings)
    except OSError as exc:
        LOG.warning("local lie service: the online card could not be written (%s)", exc)
        return False
    with _lock:
        _state["online_card"] = card
        _state["card_checked"] = True
    LOG.info(
        "local lie service: CT card from the activation server written to %s (%s)",
        path, _mask(card),
    )
    # The card is the gate for pre-starting: now that it exists, a 本地测谎
    # selection may start the service (no blind start without it).
    note_online_card(card)
    with _lock:
        started_by_app = bool(_state.get("started_by_app"))
    if started_by_app and not _busy():
        LOG.info("local lie service: restarting so the online card takes effect")
        stop_started_service()
        reset_for_tests()
        ensure_started(root)
    return True


def report_local_pass(
    event_id: str, outcome: str, *, frames: int = 0,
    latency_ms: Optional[float] = None, detail: str = "",
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
    """Which hint_text() branch applies right now: off/no_card/starting/up/failed."""

    with _lock:
        state = str(_state.get("state") or "idle")
        rejected = bool(_state.get("card_rejected"))
        desired = _desired_active
    if not desired:
        return "off"
    if not has_online_card():
        return "no_card"
    if rejected:
        return "failed"
    if state in ("running", "external"):
        return "up"
    if state == "starting":
        return "starting"
    if state in ("idle", "missing"):
        return "off"
    return "failed"

def hint_text() -> str:
    """One short sentence for the 图层校准与巡逻 hint line, or "".

    The wording is the operator's:
      * 本地测谎 selected without a CT_ card from the licence reply
            -> 本地测谎未开通
      * a card arrived but the service would not come up / validate it
            -> 本地测谎服务: 本地测谎启动失败
      * service answering /v1/health
            -> 本地测谎服务: 已启动
      * selection 关闭 or 云端测谎 (or nothing to say)
            -> no hint at all
    """

    with _lock:
        state = str(_state.get("state") or "idle")
        rejected = bool(_state.get("card_rejected"))
        desired = _desired_active
    if desired and not has_online_card():
        # The operator's short form.  The reason and the greyed state live on the
        # radio (label 本地测谎（未开通）) and in its tooltip; repeating them here made
        # the panel line carry the same fact twice.
        return "本地测谎未开通"
    if not desired and state not in ("running", "external", "starting"):
        return ""
    if rejected:
        return "本地测谎服务: 本地测谎启动失败"
    if state in ("running", "external"):
        return "本地测谎服务: 已启动"
    if state == "starting":
        return "本地测谎服务: 正在启动…"
    if state in ("idle", "missing"):
        return ""
    return "本地测谎服务: 本地测谎启动失败"

def reset_for_tests() -> None:
    with _lock:
        _state.update(state="idle", note="", root="", started_by_app=False, pids=[],
                      card_rejected=False, card_denied=False)


__all__ = [
    "SERVICE_FOLDER", "LocalLieServiceError", "activate", "ensure_started",
    "find_service_root", "hint_text", "is_running", "note_failure",
    "reset_for_tests", "service_credentials_path", "start_in_background",
    "stop_started_service", "sync_product_key", "set_busy_probe", "install_root", "write_credentials",
]
