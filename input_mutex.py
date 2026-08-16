"""Cross-process input-control mutex.

The patrol worker (assistant process) and the attack worker (YOLO
subprocess) both inject keys into the same game window.  They run in
different processes, so a ``threading.Lock`` cannot be shared; a Windows
**named mutex** provides the same mutual-exclusion guarantee across
processes.

Control ownership protocol:

- Patrol holds the mutex while it is *busy* (climbing/dropping) or while
  it sends a movement key this frame.
- Attack acquires the mutex only for the duration of a Ctrl tap, with a
  short timeout: if patrol owns input (climbing), the attack skips.

Priority (who *wants* control) is still decided by the state files
(attack_state.json / patrol_state.json); the mutex only guarantees that
the two workers never inject keys simultaneously.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

LOG = logging.getLogger("input_mutex")

# Local namespace: visible to all processes of this user/session, but not
# across sessions or to other users (safer than Global\\ on a multi-user box).
MUTEX_NAME = "Local\\MapleAssistant.InputControl.v1"

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080  # previous owner died while holding -> we own it
WAIT_TIMEOUT = 0x00000102

# SYNCHRONIZE | MUTEX_MODIFY_STATE
_MUTEX_ALL_ACCESS = 0x001F0001


class InputControlMutex:
    """A named mutex shared across processes by name."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle = None
        self._held = False
        try:
            self._handle = ctypes.windll.kernel32.CreateMutexW(
                None, False, self._name
            )
        except Exception:
            self._handle = None
        if not self._handle:
            LOG.warning("CreateMutex failed; input control NOT exclusive")

    def try_acquire(self, timeout_ms: int = 0) -> bool:
        """Try to take ownership; True when acquired (or already held)."""

        if self._held:
            return True
        if not self._handle:
            return True  # no mutex available: behave as always-owned
        result = ctypes.windll.kernel32.WaitForSingleObject(
            self._handle, int(max(0, timeout_ms))
        )
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self._held = True
            return True
        if result != WAIT_TIMEOUT:
            LOG.warning("WaitForSingleObject unexpected result=%d", result)
        return False

    def release(self) -> None:
        """Release ownership (no-op when not held)."""

        if self._held and self._handle:
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
        self._held = False

    def close(self) -> None:
        self.release()
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "InputControlMutex":
        self.try_acquire(0)
        return self

    def __exit__(self, *exc) -> None:
        self.release()


__all__ = ["InputControlMutex", "MUTEX_NAME"]
