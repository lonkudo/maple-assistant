"""Cross-instance liveness channel for the silent single-instance guard.

The named mutex keeps exactly one copy of the assistant alive, but it cannot
tell a *healthy* owner from an owner that is frozen: a copy stuck inside a
native modal dialog (``tk_messageBox`` runs its own message loop, so Tk's poll
stops while the process still owns the mutex) looks exactly like a busy one.
The operator then launches the app again, the guard refuses it, and the only
visible effect is a process that appears and vanishes - the "flash close".

This module carries the missing fact through two small files under ``work``:

* ``instance.json`` - published once by the owning copy (pid, exe, root,
  version, start time).  Every start overwrites it.
* ``instance.beat`` - mtime refreshed by the owning copy's UI poll (throttled).

A frozen poll leaves a stale beat, which lets a fresh launch recognise a
stalled owner, terminate it and take the mutex over.  Everything here is
strictly observational and silent: no dialog, no window, no console output;
the only trace is the ``work/single-instance.log`` line the caller writes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from runtime_paths import application_root

STATE_NAME = "instance.json"
BEAT_NAME = "instance.beat"

# How often the UI poll refreshes the beat.  Cheap (one os.utime) and far more
# often than any meaningful poll stall, so a frozen poll is visible quickly.
BEAT_INTERVAL_SECONDS = 2.0

# A stalled owner is only taken over after this much silence when all we know is
# that its UI poll stopped.  It has to be longer than any legitimate freeze: a
# native confirm dialog the operator is actually looking at also stops the poll,
# and killing that copy would destroy live work.
STALLED_TAKEOVER_SECONDS = 120.0

# ...but an *unseen* dialog is a different thing: the owner is blocked on a
# message box that is not in front (it ended up behind the game), so nobody can
# ever answer it.  That copy is dead weight and a relaunch must not be refused
# because of it, so short silence is enough once that shape is recognised.
UNSEEN_DIALOG_SECONDS = 10.0

# A main window that does not answer ``WM_NULL`` is the cheapest possible proof
# that the copy is wedged, and Windows itself calls a window hung after 5 s.  The
# field case that forced this: a restarted instance froze on the licence step in
# bursts (a 3 s HTTP probe held a lock the Tk thread wanted), so its beat stayed
# only a few seconds stale - above that, the old 10 s rule refused the second
# launch (the "flash close") and the third one worked once the freeze ended.
UNRESPONSIVE_WINDOW_SECONDS = 5.0

# Owner that published NO liveness record at all (an instance started by a build
# from before this channel existed, or a record lost before the first publish).
# Its mutex ownership is all we have, so the verdict comes from the process
# itself: a healthy instance shows its window within seconds, therefore a
# two-minute-old process with no window at all is a zombie.  A window that
# exists but does not answer ``WM_NULL`` is taken as stalled immediately.
NO_WINDOW_TAKEOVER_SECONDS = 120.0

# Image names this product has shipped under; used only to *find* candidates when
# no record exists.  A candidate is never touched unless its image sits in THIS
# installation folder.
IMAGE_NAME_PREFIXES = ("mooneclipse", "todohelper", "mapleassistant")

# Access rights.
_SYNCHRONIZE = 0x00100000
_TERMINATE = 0x0001
_QUERY_LIMITED = 0x1000

# SendMessageTimeout flags: abort if the window is hung, and block the caller.
_SMTO_BLOCK_ABORTIFHUNG = 0x0003
_WM_NULL = 0x0000
_DIALOG_CLASS = "#32770"

_last_beat = 0.0
_dlls: dict[str, Any] = {}


def _kernel32():  # noqa: ANN202 - ctypes handle
    """kernel32 with fully declared prototypes.

    Every argument and return type is declared on purpose: an undeclared HANDLE
    is marshalled as a 32-bit C int, which truncates a 64-bit handle and makes a
    dead process look alive (or a live one look gone).
    """

    cached = _dlls.get("kernel32")
    if cached is not None:
        return cached
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.CloseHandle.restype = wintypes.BOOL
    dll.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    dll.TerminateProcess.restype = wintypes.BOOL
    dll.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    dll.QueryFullProcessImageNameW.restype = wintypes.BOOL
    dll.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    )
    dll.GetProcessTimes.restype = wintypes.BOOL
    _dlls["kernel32"] = dll
    return dll


def _window_enum_proc():  # noqa: ANN202 - ctypes callback type
    """The ``WNDENUMPROC`` type (declared once, shared by prototype and call)."""

    cached = _dlls.get("wndenumproc")
    if cached is not None:
        return cached
    import ctypes
    from ctypes import wintypes

    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _dlls["wndenumproc"] = proc_type
    return proc_type


def _user32():  # noqa: ANN202 - ctypes handle
    """user32 with fully declared prototypes (see :func:`_kernel32`)."""

    cached = _dlls.get("user32")
    if cached is not None:
        return cached
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("user32", use_last_error=True)
    dll.EnumWindows.argtypes = (_window_enum_proc(), wintypes.LPARAM)
    dll.EnumWindows.restype = wintypes.BOOL
    dll.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    dll.GetWindowThreadProcessId.restype = wintypes.DWORD
    dll.IsWindowVisible.argtypes = (wintypes.HWND,)
    dll.IsWindowVisible.restype = wintypes.BOOL
    dll.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    dll.GetWindowRect.restype = wintypes.BOOL
    dll.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    dll.GetClassNameW.restype = ctypes.c_int
    dll.GetForegroundWindow.argtypes = ()
    dll.GetForegroundWindow.restype = wintypes.HWND
    dll.SendMessageTimeoutW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    )
    dll.SendMessageTimeoutW.restype = ctypes.c_size_t
    _dlls["user32"] = dll
    return dll


def work_dir(root: Path | str | None = None) -> Path:
    """Directory that holds the two liveness files."""

    base = Path(root) if root is not None else application_root()
    return base / "work"


def state_path(root: Path | str | None = None) -> Path:
    return work_dir(root) / STATE_NAME


def beat_path(root: Path | str | None = None) -> Path:
    return work_dir(root) / BEAT_NAME


def publish(pid: int | None = None, *, version: str = "", root: Path | str | None = None) -> None:
    """Record this process as the owning copy and start its beat.

    Called right after the single-instance mutex is acquired.  Best effort: a
    read-only install folder must never stop the assistant from starting.
    """

    global _last_beat
    try:
        folder = work_dir(root)
        folder.mkdir(parents=True, exist_ok=True)
        base = Path(root) if root is not None else application_root()
        payload = {
            "pid": int(pid if pid is not None else os.getpid()),
            "exe": _own_exe(),
            "root": str(base),
            "version": str(version or ""),
            "started": time.time(),
        }
        state_path(root).write_text(
            json.dumps(payload, ensure_ascii=True), encoding="utf-8",
        )
        touch_beat(root=root, force=True)
    except Exception:  # noqa: BLE001 - liveness bookkeeping is optional
        pass


def touch_beat(*, root: Path | str | None = None, force: bool = False) -> None:
    """Refresh the beat; the UI poll calls this on every tick (throttled)."""

    global _last_beat
    now = time.monotonic()
    if not force and (now - _last_beat) < BEAT_INTERVAL_SECONDS:
        return
    _last_beat = now
    try:
        path = beat_path(root)
        if path.exists():
            os.utime(path, None)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001 - never disturb a poll tick
        pass


def read_state(root: Path | str | None = None) -> Optional[dict[str, Any]]:
    """The owning copy's record, or ``None`` when there is none/unreadable."""

    try:
        value = json.loads(state_path(root).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:  # noqa: BLE001 - a missing record is a normal state
        return None


def beat_age_seconds(root: Path | str | None = None) -> Optional[float]:
    """Seconds since the owner's last UI poll, or ``None`` when unknown."""

    try:
        return max(0.0, time.time() - beat_path(root).stat().st_mtime)
    except Exception:  # noqa: BLE001 - no beat yet
        return None


def process_alive(pid: int) -> bool:
    """Whether ``pid`` still exists (handle probe, no psutil)."""

    if pid <= 0:
        return False
    try:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            # WAIT_OBJECT_0 means the process has exited; WAIT_TIMEOUT (258)
            # means it is still running.
            return kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - unknown means "treat as gone"
        return False


def process_image_path(pid: int) -> Optional[str]:
    """Full image path of ``pid``, or ``None`` when it cannot be read."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_QUERY_LIMITED, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return str(buffer.value) or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def process_age_seconds(pid: int) -> Optional[float]:
    """Seconds since ``pid`` was created, or ``None`` when it cannot be read."""

    if pid <= 0:
        return None
    try:
        import ctypes
        import datetime
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_QUERY_LIMITED, False, int(pid))
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel_t), ctypes.byref(user_t),
            ):
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            born = datetime.datetime(1601, 1, 1) + datetime.timedelta(
                microseconds=ticks // 10,
            )
            now = datetime.datetime.utcnow()
            return max(0.0, (now - born).total_seconds())
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def assistant_pids() -> list[int]:
    """Pids of other processes carrying one of this product's image names."""

    pids: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snapshot:
            return pids
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    image = str(entry.szExeFile).casefold()
                    if image.startswith(IMAGE_NAME_PREFIXES):
                        pid = int(entry.th32ProcessID)
                        if pid != os.getpid():
                            pids.append(pid)
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # noqa: BLE001 - diagnostics only
        return pids
    return pids


def _is_this_install(pid: int, root: Path | str | None = None) -> bool:
    """Whether ``pid`` runs the executable of THIS installation folder."""

    image = process_image_path(pid)
    if not image:
        return False
    base = Path(root) if root is not None else application_root()
    try:
        return os.path.normcase(str(Path(image).parent)) == os.path.normcase(str(base))
    except Exception:  # noqa: BLE001 - diagnostics only
        return False


def describe_process(pid: int) -> dict[str, Any]:
    """Everything the duplicate-instance log needs about one candidate."""

    window = main_window_of_pid(pid)
    dialog = dialog_window_of_pid(pid)
    return {
        "pid": pid,
        "alive": process_alive(pid),
        "image": process_image_path(pid),
        "same_install": _is_this_install(pid),
        "age": process_age_seconds(pid),
        "window": window,
        "responding": window_responding(window) if window is not None else None,
        "dialog": dialog,
        "foreground": foreground_pid(),
    }


def _visible_windows_of_pid(pid: int) -> list[tuple[int, str, int]]:
    """``(area, class, hwnd)`` for every visible top-level window of ``pid``."""

    try:
        import ctypes
        from ctypes import wintypes

        user32 = _user32()
        proc_type = _window_enum_proc()
        found: list[tuple[int, str, int]] = []
        target = int(pid)

        def callback(hwnd, _param):  # type: ignore[no-untyped-def]
            try:
                owner = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
                if int(owner.value) != target:
                    return True
                if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
                    return True
                rect = wintypes.RECT()
                if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                    return True
                width = int(rect.right) - int(rect.left)
                height = int(rect.bottom) - int(rect.top)
                if width <= 0 or height <= 0:
                    return True
                found.append((width * height, _class_name(int(hwnd)), int(hwnd)))
            except Exception:  # noqa: BLE001 - keep enumerating
                return True
            return True

        user32.EnumWindows(proc_type(callback), 0)
        return found
    except Exception:  # noqa: BLE001 - diagnostics only
        return []


def _class_name(hwnd: int) -> str:
    """Window class name (``#32770`` for a native dialog)."""

    try:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(256)
        _user32().GetClassNameW(wintypes.HWND(int(hwnd)), buffer, 256)
        return str(buffer.value)
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""


def main_window_of_pid(pid: int) -> Optional[int]:
    """Largest visible non-dialog top-level window of ``pid``.

    Native dialogs (class ``#32770``) are excluded on purpose: a message box has
    its own message loop, so pinging it reports "responsive" while the window
    that actually stopped working is the main one.
    """

    windows = [
        entry for entry in _visible_windows_of_pid(pid)
        if entry[1] != _DIALOG_CLASS
    ]
    if not windows:
        return None
    windows.sort()
    return windows[-1][2]


def dialog_window_of_pid(pid: int) -> Optional[int]:
    """Topmost visible native dialog (``#32770``) of ``pid``, if any."""

    dialogs = [
        entry for entry in _visible_windows_of_pid(pid)
        if entry[1] == _DIALOG_CLASS
    ]
    if not dialogs:
        return None
    dialogs.sort()
    return dialogs[-1][2]


def foreground_pid() -> Optional[int]:
    """Pid that owns the foreground window, or ``None``."""

    try:
        import ctypes
        from ctypes import wintypes

        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
        return int(owner.value) or None
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def window_responding(hwnd: int, timeout_ms: int = 1000) -> bool:
    """Whether ``hwnd`` answers a ``WM_NULL`` ping within ``timeout_ms``."""

    try:
        import ctypes
        from ctypes import wintypes

        result = ctypes.c_size_t(0)
        sent = _user32().SendMessageTimeoutW(
            wintypes.HWND(int(hwnd)), _WM_NULL, 0, 0, _SMTO_BLOCK_ABORTIFHUNG,
            int(timeout_ms), ctypes.byref(result),
        )
        return bool(sent)
    except Exception:  # noqa: BLE001 - unknown means "assume responsive"
        return True


def terminate(pid: int, *, wait_seconds: float = 3.0) -> bool:
    """Terminate ``pid`` and wait until it is really gone.

    Refuses the calling process.  A takeover that names its own pid (a reused pid,
    a stale record, a helper that resolved the wrong process) would kill the very
    instance that is running the takeover: the app vanishes with no traceback and
    nothing in any log, which is indistinguishable from the operator's flash close.
    """

    if pid <= 0:
        return False
    if int(pid) == os.getpid():
        try:
            logging.getLogger(__name__).error(
                "instance state: refusing to terminate this very process (pid=%s)",
                pid,
            )
        except Exception:  # noqa: BLE001 - a logging failure changes nothing
            pass
        return False
    try:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_TERMINATE | _SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            kernel32.TerminateProcess(handle, 1)
            kernel32.WaitForSingleObject(handle, int(max(0.0, wait_seconds) * 1000))
        finally:
            kernel32.CloseHandle(handle)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while time.monotonic() < deadline:
            if not process_alive(pid):
                return True
            time.sleep(0.1)
        return not process_alive(pid)
    except Exception:  # noqa: BLE001 - a failed takeover falls back to silence
        return False


def stalled_owner(*, min_stale_seconds: float = STALLED_TAKEOVER_SECONDS,
                  root: Path | str | None = None) -> Optional[dict[str, Any]]:
    """Return the owning copy when it is stalled, or ``None``.

    A candidate must be: the same installation, still alive, and not doing UI
    work.  With a published record three shapes count:

    * **no UI poll for** ``min_stale_seconds`` - the whole poll stopped, which is
      what happens when Tk is blocked inside a native message box;
    * **an unseen dialog** - the owner has a modal dialog that is not in front
      (it landed behind the game), so nobody will ever answer it;
      ``UNSEEN_DIALOG_SECONDS`` of silence is enough there;
    * **an unresponsive window** - same short allowance.

    A responding main window means live work and is never interrupted.

    Without a record - an instance started by a build older than this channel,
    which is exactly what the field log showed ("owning instance pid(s): 7908"
    with no ``owner state:`` line) - the candidates come from the process table
    and the verdict comes from the process itself: no window at all after
    ``NO_WINDOW_TAKEOVER_SECONDS``, or a window that does not answer ``WM_NULL``.
    """

    state = read_state(root)
    if state:
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        verdict = _verdict(
            pid, root, state=state, beat_age=beat_age_seconds(root),
            min_stale_seconds=min_stale_seconds,
        )
        if verdict is not None:
            return verdict
        if pid > 0 and pid != os.getpid() and process_alive(pid):
            # The recorded owner is alive and looks healthy; do not go hunting
            # for another pid to kill.
            return None
    for pid in assistant_pids():
        verdict = _verdict(
            pid, root, state=None, beat_age=None,
            min_stale_seconds=min_stale_seconds,
        )
        if verdict is not None:
            return verdict
    return None


def _verdict(
    pid: int,
    root: Path | str | None,
    *,
    state: Optional[dict[str, Any]],
    beat_age: Optional[float],
    min_stale_seconds: float,
) -> Optional[dict[str, Any]]:
    """Decide whether ``pid`` is a stalled owner worth taking over."""

    if pid <= 0 or pid == os.getpid():
        return None
    if not process_alive(pid):
        return None
    same_install = _is_this_install(pid, root) or (
        state is not None and _same_install(state, root)
    )
    if not same_install:
        return None
    window = main_window_of_pid(pid)
    responding = window_responding(window) if window is not None else None
    if responding:
        return None
    dialog = dialog_window_of_pid(pid)
    unseen = dialog is not None and foreground_pid() != pid
    age = process_age_seconds(pid)
    reason: Optional[str] = None
    if beat_age is not None:
        if beat_age >= float(min_stale_seconds):
            reason = "no-ui-poll"
        elif unseen and beat_age >= UNSEEN_DIALOG_SECONDS:
            reason = "unseen-dialog"
        elif window is not None and beat_age >= UNRESPONSIVE_WINDOW_SECONDS:
            reason = "window-unresponsive"
    elif window is not None:
        # No record at all: the window exists and did not answer WM_NULL.
        reason = "window-unresponsive"
    elif age is not None and age >= NO_WINDOW_TAKEOVER_SECONDS:
        # No record and no window: a healthy copy shows its window in seconds.
        reason = "no-window"
    if reason is None:
        return None
    return {
        "pid": pid,
        "beat_age": float(beat_age) if beat_age is not None else None,
        "window": window,
        "responding": responding,
        "unseen_dialog": unseen,
        "age": age,
        "reason": reason,
        "exe": (state or {}).get("exe") or process_image_path(pid),
        "version": (state or {}).get("version"),
    }


def _same_install(state: dict[str, Any], root: Path | str | None = None) -> bool:
    """Whether ``state`` describes a copy of THIS installation."""

    base = Path(root) if root is not None else application_root()
    recorded_root = str(state.get("root") or "").strip()
    if recorded_root:
        try:
            if os.path.normcase(recorded_root) != os.path.normcase(str(base)):
                return False
        except Exception:  # noqa: BLE001 - fall through to the exe check
            return False
    recorded_exe = str(state.get("exe") or "").strip()
    if not recorded_exe:
        return bool(recorded_root)
    try:
        if os.path.normcase(recorded_exe) == os.path.normcase(_own_exe()):
            return True
    except Exception:  # noqa: BLE001 - compare the roots only
        pass
    return bool(recorded_root)


def _own_exe() -> str:
    """The executable path this copy runs from (as the OS reports it)."""

    try:
        return os.path.abspath(sys.executable)
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "BEAT_INTERVAL_SECONDS", "IMAGE_NAME_PREFIXES", "NO_WINDOW_TAKEOVER_SECONDS",
    "STALLED_TAKEOVER_SECONDS", "UNSEEN_DIALOG_SECONDS",
    "assistant_pids", "beat_age_seconds", "beat_path", "describe_process",
    "dialog_window_of_pid", "foreground_pid", "main_window_of_pid",
    "process_age_seconds", "process_alive", "process_image_path", "publish",
    "read_state", "stalled_owner", "state_path", "terminate", "touch_beat",
    "window_responding", "work_dir",
]
