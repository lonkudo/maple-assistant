"""One Esc cancellation watcher for workflows that temporarily own the game.

Esc is deliberately polled only while one of the registered workflows is
active.  It is not a permanent global hotkey, so the game's ordinary Esc
behaviour remains untouched at all other times.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable, Sequence


LOG = logging.getLogger(__name__)
VK_ESCAPE = 0x1B


class WorkflowCancelWorker(threading.Thread):
    """Cancel active reconnect, restart, auto-lie, and Ctrl+Q workflows on Esc."""

    POLL_SECONDS = 0.025

    def __init__(
        self,
        stop_event: threading.Event,
        workflows: Sequence[tuple[str, Callable[[], bool], Callable[[], bool]]],
    ) -> None:
        super().__init__(name="workflow-cancel", daemon=True)
        self.stop_event = stop_event
        self.workflows = tuple(workflows)
        self._was_pressed = False

    @staticmethod
    def _escape_pressed() -> bool:
        if sys.platform != "win32":
            return False
        try:
            return bool(
                ctypes.WinDLL("user32", use_last_error=True)
                .GetAsyncKeyState(VK_ESCAPE) & 0x8000
            )
        except Exception:
            LOG.debug("workflow cancel: Esc state could not be read", exc_info=True)
            return False

    def run(self) -> None:
        LOG.info("workflow cancel worker started (Esc watches reconnect, restart, auto-lie, Ctrl+Q trade)")
        while not self.stop_event.wait(self.POLL_SECONDS):
            active = []
            for name, is_active, cancel in self.workflows:
                try:
                    if is_active():
                        active.append((name, cancel))
                except Exception:
                    LOG.debug("workflow cancel: active check failed for %s", name, exc_info=True)
            pressed = self._escape_pressed() if active else False
            if pressed and not self._was_pressed:
                cancelled = []
                for name, cancel in active:
                    try:
                        if cancel():
                            cancelled.append(name)
                    except Exception:
                        LOG.exception("workflow cancel: could not cancel %s", name)
                if cancelled:
                    LOG.warning("workflow cancel: Esc cancelled %s", ", ".join(cancelled))
            self._was_pressed = pressed
        LOG.info("workflow cancel worker stopped")


__all__ = ["WorkflowCancelWorker"]
