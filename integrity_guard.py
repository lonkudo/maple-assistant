"""Process integrity: why injected keys can be silently dropped.

Windows' UIPI rule: a process may only inject input into a window of the SAME or
LOWER integrity level.  When the game runs elevated (high) and the assistant was
started normally (medium), every attack tap is delivered by the OS with no error
and no effect - the log shows a perfectly normal beat while the character never
attacks.  That is exactly the 2026-11-16 field report: "the restarted instance
right after the update does not attack in either mode".

The launcher already asks for elevation (``ShellExecute … "runas"``) for this
reason.  A restart performed by the update helper, an installer, or by hand can
lose that level, so the app now REPORTS it instead of leaving the operator with a
silent "no attack":

    PROCESS INTEGRITY: this instance runs elevated / not elevated
    KEY INJECTION BLOCKED: the game window runs at a HIGHER integrity level …

Nothing here changes how keys are sent; it makes the mismatch visible.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional, Tuple

LOG = logging.getLogger(__name__)

# Token integrity RIDs (winnt.h): medium is the default for a normal user
# process, high is what "run as administrator" produces.
INTEGRITY_NAMES = {
    0x0000: "untrusted",
    0x1000: "low",
    0x2000: "medium",
    0x2100: "medium-plus",
    0x3000: "high",
    0x4000: "system",
}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def _prototypes() -> Tuple[object, object, object]:
    """Declare the prototypes once: without argtypes the 64-bit handles truncate."""

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    user32 = ctypes.windll.user32

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    return kernel32, advapi32, user32


KERNEL32, ADVAPI32, USER32 = _prototypes()


def _token_integrity(process_handle: int) -> Optional[int]:
    """The last SID sub-authority of the process token's integrity SID."""

    token = wintypes.HANDLE()
    if not ADVAPI32.OpenProcessToken(
        wintypes.HANDLE(process_handle), TOKEN_QUERY, ctypes.byref(token)
    ):
        return None
    try:
        size = wintypes.DWORD(0)
        ADVAPI32.GetTokenInformation(
            token, TokenIntegrityLevel, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not ADVAPI32.GetTokenInformation(
            token, TokenIntegrityLevel, buffer, size, ctypes.byref(size)
        ):
            return None
        label = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
        count_pointer = ADVAPI32.GetSidSubAuthorityCount(label.Label.Sid)
        if not count_pointer:
            return None
        count = int(count_pointer.contents.value)
        if count <= 0:
            return None
        authority = ADVAPI32.GetSidSubAuthority(label.Label.Sid, count - 1)
        if not authority:
            return None
        return int(authority.contents.value)
    finally:
        KERNEL32.CloseHandle(token)


def current_integrity() -> Optional[int]:
    """This process's integrity RID, or None when Windows would not say."""

    try:
        return _token_integrity(KERNEL32.GetCurrentProcess())
    except Exception:  # noqa: BLE001 - a diagnostic must never break startup
        LOG.debug("integrity: the current token could not be read", exc_info=True)
        return None


def window_process_id(hwnd: int) -> Optional[int]:
    """The owning process of one window handle."""

    if not hwnd:
        return None
    try:
        pid = wintypes.DWORD(0)
        USER32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:  # noqa: BLE001
        return None


def process_integrity(pid: Optional[int] = None) -> Optional[int]:
    """Another process's integrity RID (this process when ``pid`` is None)."""

    if pid is None:
        return current_integrity()
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return _token_integrity(handle)
    except Exception:  # noqa: BLE001
        LOG.debug("integrity: the target token could not be read", exc_info=True)
        return None
    finally:
        KERNEL32.CloseHandle(handle)


def describe(integrity: Optional[int]) -> str:
    """Human label for a RID, including the level Windows uses by default."""

    if integrity is None:
        return "unknown"
    name = INTEGRITY_NAMES.get(integrity)
    if name:
        return f"{name} (0x{integrity:04X})"
    return f"0x{integrity:04X}"


def is_elevated() -> bool:
    """True when this process runs with an administrator token."""

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def startup_note() -> str:
    """One log-safe line describing this instance's own rights."""

    integrity = current_integrity()
    state = "elevated" if is_elevated() else "not elevated"
    return f"{state}, integrity {describe(integrity)}"


def injection_blocker(hwnd: Optional[int]) -> Optional[str]:
    """Why injected keys cannot reach *hwnd*, or None when they can.

    Only the UIPI direction is reported: a game at a HIGHER integrity than this
    process silently discards every injected key.  Same level and lower levels
    are fine.
    """

    pid = window_process_id(int(hwnd)) if hwnd else None
    if not pid:
        return None
    ours = current_integrity()
    theirs = process_integrity(pid)
    if ours is None or theirs is None or theirs <= ours:
        return None
    return (
        f"the game window (pid {pid}) runs at integrity {describe(theirs)} while this "
        f"assistant runs at {describe(ours)}; Windows drops injected keys across that "
        "boundary, so attacks/hotkeys will do nothing even though the log shows the "
        "beat - close this instance and start it with 启动助手.bat, which asks for "
        "administrator rights"
    )


__all__ = [
    "INTEGRITY_NAMES", "current_integrity", "describe", "injection_blocker",
    "is_elevated", "process_integrity", "startup_note", "window_process_id",
]
