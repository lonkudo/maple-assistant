"""Foreground-window gate for gameplay workers; the UI remains independent."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional


LOG = logging.getLogger(__name__)


class FocusWorker(threading.Thread):
    """Pause automation whenever the selected game is not foreground.

    A short focus dip (dialog, notification, taskbar flicker) is tolerated:
    keyboard input pauses while the game is away, and automation resumes
    silently if the game comes back within ``focus_lost_grace_seconds``.
    Only a sustained loss (the user actually switched away) terminally stops
    the run, so transient steals no longer kill the patrol.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        automation_active_event: threading.Event,
        game_focused_event: threading.Event,
        poll_interval: float = 0.20,
        focus_lost_grace_seconds: float = 2.5,
        refocus_interval_seconds: float = 1.0,
        on_focus_lost: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="focus-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.automation_active_event = automation_active_event
        self.game_focused_event = game_focused_event
        self.poll_interval = max(0.05, float(poll_interval))
        self.focus_lost_grace_seconds = max(0.0, float(focus_lost_grace_seconds))
        self.refocus_interval_seconds = max(0.25, float(refocus_interval_seconds))
        self.on_focus_lost = on_focus_lost
        self._lost_since: Optional[float] = None
        self._last_refocus_at: Optional[float] = None
        self._refocus_in_flight = False

    def _try_refocus(self) -> None:
        """Bring the game window back to the foreground (async, rate-limited).

        Runs in a short-lived daemon thread so the poll loop keeps releasing
        keys and stays responsive; ``select_window`` may retry for ~1-2s.
        """
        if self._refocus_in_flight:
            return
        now = time.monotonic()
        if (self._last_refocus_at is not None
                and now - self._last_refocus_at < self.refocus_interval_seconds):
            return
        self._last_refocus_at = now
        self._refocus_in_flight = True

        def _refocus_worker() -> None:
            try:
                if self.key_sender.select_window():
                    LOG.info(
                        "AUTOMATION REFOCUS: game window brought back to "
                        "foreground"
                    )
                else:
                    LOG.debug("auto-refocus: select_window returned False")
            except Exception as exc:
                LOG.debug("auto-refocus failed: %s", exc)
            finally:
                self._refocus_in_flight = False

        threading.Thread(target=_refocus_worker, name="focus-refocus",
                         daemon=True).start()

    def _log_foreground_snapshot(self) -> None:
        """Best-effort log of what window currently holds the foreground."""
        try:
            import win32gui

            fg = win32gui.GetForegroundWindow()
            if not fg:
                LOG.warning("game lost focus: no foreground window")
                return
            title = win32gui.GetWindowText(fg)
            cls = win32gui.GetClassName(fg)
            LOG.warning(
                "game lost focus; foreground now: title=%r class=%r hwnd=%s",
                title[:80], cls, fg,
            )
        except Exception as exc:  # pragma: no cover - diagnostics only
            LOG.debug("could not snapshot foreground window: %s", exc)

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
                now = time.monotonic()
                if active:
                    if self._lost_since is not None:
                        # Focus returned inside the grace window: resume
                        # without a terminal stop.
                        LOG.info(
                            "AUTOMATION RESUMED after %.2fs focus dip",
                            now - self._lost_since,
                        )
                        self._lost_since = None
                    previous = active
                elif input_enabled:
                    # Game not focused while keyboard input is armed.  First
                    # try to restore the foreground automatically; the grace
                    # timer then decides between a transient dip (resume) and
                    # a sustained loss (terminal stop) if refocus fails.
                    self._try_refocus()
                    if self._lost_since is None:
                        self._lost_since = now
                        self._log_foreground_snapshot()
                    if now - self._lost_since >= self.focus_lost_grace_seconds:
                        if previous is True:
                            self.key_sender.disable_input()
                            if self.on_focus_lost is not None:
                                try:
                                    self.on_focus_lost()
                                except Exception:
                                    LOG.exception(
                                        "could not mark patrol stopped after "
                                        "focus loss"
                                    )
                        LOG.warning(
                            "AUTOMATION STOPPED: game window lost focus for "
                            "%.1fs; UI stays active",
                            now - self._lost_since,
                        )
                        self._lost_since = None
                        previous = False
                else:
                    # Keyboard input disabled (patrol stopped by the user):
                    # idle, nothing to gate.
                    previous = False
                    self._lost_since = None
                if self.stop_event.wait(self.poll_interval):
                    break
        finally:
            self.automation_active_event.clear()
            self.game_focused_event.clear()
            self.key_sender.release_all_keys()
            LOG.info("focus worker stopped")


__all__ = ["FocusWorker"]
