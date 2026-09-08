"""Manual-only rapid Z pickup controlled by the Ctrl+Z hotkey."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable


LOG = logging.getLogger(__name__)


class QuickPickupWorker(threading.Thread):
    """Toggle short Z taps without arming the patrol automation workers.

    The worker deliberately uses ``send_direct_keys`` rather than the normal
    live-input gate.  This lets manual pickup run while patrol is stopped
    without also waking attack, movement, or status automation.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        result_queue: "queue.Queue[tuple[str, str]]",
        *,
        patrol_running: Callable[[], bool],
        interval_seconds: float = 0.15,
    ) -> None:
        super().__init__(name="quick-pickup-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.result_queue = result_queue
        self.patrol_running = patrol_running
        self.interval_seconds = max(0.10, float(interval_seconds))
        self._requests: "queue.Queue[str]" = queue.Queue(maxsize=2)
        self._active = threading.Event()

    def request_toggle(self) -> bool:
        """Ask the dedicated worker to start or stop, without blocking Tk."""

        try:
            self._requests.put_nowait("toggle")
            return True
        except queue.Full:
            LOG.warning("quick pickup toggle ignored: request already pending")
            return False

    def is_active(self) -> bool:
        return self._active.is_set()

    def _report(self, state: str, detail: str = "") -> None:
        LOG.info("quick pickup %s%s", state, f": {detail}" if detail else "")
        try:
            self.result_queue.put_nowait((state, detail))
        except queue.Full:
            LOG.warning("quick pickup result dropped: %s", state)

    def _start(self) -> None:
        if self.patrol_running():
            self._report("failed", "patrol is running")
            return
        try:
            if self.key_sender.select_window() is False:
                self._report("failed", "game window selection failed")
                return
            if not self.key_sender.is_game_foreground():
                self._report("failed", "game window is not foreground")
                return
        except Exception as exc:
            LOG.exception("quick pickup game window selection failed")
            self._report("failed", str(exc))
            return
        self._active.set()
        self._report("started")

    def _handle_requests(self) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                if request == "toggle":
                    if self._active.is_set():
                        self._active.clear()
                        self._report("stopped")
                    else:
                        self._start()
            finally:
                self._requests.task_done()

    def run(self) -> None:
        LOG.info("quick pickup worker started interval=%.2fs", self.interval_seconds)
        try:
            while not self.stop_event.is_set():
                self._handle_requests()
                if self._active.is_set():
                    if self.patrol_running():
                        self._active.clear()
                        self._report("stopped", "patrol started")
                    elif not self.key_sender.is_game_foreground():
                        self._active.clear()
                        self._report("failed", "game window lost foreground")
                    else:
                        try:
                            sent = self.key_sender.send_direct_keys("z") is not False
                        except Exception:
                            LOG.exception("quick pickup Z tap failed")
                            sent = False
                        if not sent:
                            self._active.clear()
                            self._report("failed", "Z input was not sent")
                self.stop_event.wait(self.interval_seconds if self._active.is_set() else 0.05)
        finally:
            self._active.clear()
        LOG.info("quick pickup worker stopped")


__all__ = ["QuickPickupWorker"]
