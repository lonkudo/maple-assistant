"""One-shot traceback capture when Tk stops making progress.

The watchdog is reset by every entered UI poll.  Resetting does not write to
disk; only a genuine stalled UI writes a Python thread dump.  This preserves
evidence for an Application Hang event, where Windows terminates pythonw.exe
without giving normal exception handlers a chance to run.
"""

from __future__ import annotations

import faulthandler
import threading
from pathlib import Path


class UiHangWatchdog:
    """Write one all-thread traceback after a sustained Tk stall."""

    def __init__(self, path: Path, *, timeout_seconds: float = 8.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._lock = threading.Lock()
        self._stream = None
        self._active = False

    def start(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._stream = self.path.open("a", encoding="utf-8", buffering=1)
                self._active = True
            self.kick()
        except Exception:
            # Hang diagnosis is strictly observational; a read-only or broken
            # log folder must never prevent the assistant itself from opening.
            self.close()

    def kick(self) -> None:
        """Reset the one-shot timer without writing a normal-operation log."""

        with self._lock:
            if not self._active or self._stream is None:
                return
            try:
                faulthandler.dump_traceback_later(
                    self.timeout_seconds,
                    repeat=False,
                    file=self._stream,
                    exit=False,
                )
            except Exception:
                self._active = False

    def close(self) -> None:
        with self._lock:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass
            stream = self._stream
            self._stream = None
            self._active = False
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


__all__ = ["UiHangWatchdog"]
