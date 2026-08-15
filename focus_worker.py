"""Foreground-window gate for gameplay workers; the UI remains independent."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional


LOG = logging.getLogger(__name__)


class FocusWorker(threading.Thread):
    """Pause automation whenever the selected game is not foreground."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        automation_active_event: threading.Event,
        game_focused_event: threading.Event,
        poll_interval: float = 0.20,
        on_focus_lost: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="focus-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.automation_active_event = automation_active_event
        self.game_focused_event = game_focused_event
        self.poll_interval = max(0.05, float(poll_interval))
        self.on_focus_lost = on_focus_lost

    def run(self) -> None:
        previous: bool | None = None
        try:
            while not self.stop_event.is_set():
                input_enabled = bool(self.key_sender.input_is_enabled())
                game_focused = bool(self.key_sender.is_game_foreground())
                if game_focused:
                    self.game_focused_event.set()
                else:
                    self.game_focused_event.clear()
                active = bool(input_enabled and game_focused)
                if active:
                    self.automation_active_event.set()
                else:
                    self.automation_active_event.clear()
                    self.key_sender.release_all_keys()
                # Losing the selected game is a terminal stop for the current
                # patrol run. Do not silently resume keyboard automation when
                # the user later returns focus to the game.
                if previous is True and not active and input_enabled:
                    self.key_sender.disable_input()
                    if self.on_focus_lost is not None:
                        try:
                            self.on_focus_lost()
                        except Exception:
                            LOG.exception("could not mark patrol stopped after focus loss")
                if active != previous:
                    if active:
                        LOG.info("AUTOMATION RESUMED: game window is foreground")
                    elif input_enabled:
                        LOG.warning(
                            "AUTOMATION STOPPED: game window lost focus; UI stays active"
                        )
                    previous = active
                if self.stop_event.wait(self.poll_interval):
                    break
        finally:
            self.automation_active_event.clear()
            self.game_focused_event.clear()
            self.key_sender.release_all_keys()
            LOG.info("focus worker stopped")


__all__ = ["FocusWorker"]
