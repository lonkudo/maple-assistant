"""Foreground-window gate for gameplay workers; the UI remains independent."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class FocusWorker(threading.Thread):
    """Pause automation whenever the selected game is not foreground.

    A focus loss NEVER ends a run: the operator's rule is that only a manual
    Stop Patrol ends a patrol, so automation pauses while the game is away and
    resumes by itself the moment the game is focused again - whether the dip
    lasted 0.1s or an hour.  While paused the direction keys are released, and
    the input boundary refuses every key while the game is not foreground, so
    nothing can be delivered into whatever window took the focus.

    ``focus_lost_grace_seconds`` is the point at which the pause is *reported*;
    a shorter dip resumes silently.
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
        lie_pass_event: Optional[threading.Event] = None,
        reconnect_active_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="focus-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.automation_active_event = automation_active_event
        self.game_focused_event = game_focused_event
        # Set while a lie pass owns the machine.  Such a pass runs with input
        # DISARMED, but its own capture (the ~30 fps lie cadence) still needs the
        # foreground gate open - so the real foreground state keeps being probed
        # for the whole pass instead of being inferred from keyboard ownership.
        self.lie_pass_event = lie_pass_event
        # Set while the auto-reconnect owns the machine: the automation switch must stay CLEARED then,
        # otherwise this worker sets it again 200 ms later and attack/jump start on the login page.
        self.reconnect_active_event = reconnect_active_event
        self.poll_interval = max(0.05, float(poll_interval))
        self.focus_lost_grace_seconds = max(0.0, float(focus_lost_grace_seconds))
        self.refocus_interval_seconds = max(0.25, float(refocus_interval_seconds))
        self._lost_since: Optional[float] = None
        self._last_refocus_at: Optional[float] = None
        self._refocus_in_flight = False
        # The pause is reported once per unfocused episode, not every poll.
        self._pause_reported = False

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
        """Best-effort log of what window currently holds the foreground.

        A stolen foreground is the usual reason a short action tap (jump/buff)
        dies silently: ``SendInput`` is global, so the keystroke lands in that
        window instead of the game.  The snapshot therefore names the process
        as well as the window class, so the operator can close the offender.
        """

        describe = getattr(self.key_sender, "describe_foreground", None)
        if callable(describe):
            try:
                LOG.warning("game lost focus; foreground now: %s", describe())
                return
            except Exception as exc:  # pragma: no cover - diagnostics only
                LOG.debug("could not describe foreground window: %s", exc)
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
                # Stay completely idle while patrol is stopped.  The old
                # unconditional probe searched for the game every 0.2s and
                # also kept the capture pipeline running before Start Patrol.
                # A lie pass is the one exception: it owns the machine with
                # input disabled, yet the pass itself is fed by that same
                # capture, so the foreground state is still probed then (keys
                # stay disarmed - ``active`` below is unchanged).
                lie_pass = bool(
                    self.lie_pass_event is not None
                    and self.lie_pass_event.is_set()
                )
                game_focused = (
                    bool(self.key_sender.is_game_foreground())
                    if (input_enabled or lie_pass) else False
                )
                if game_focused:
                    self.game_focused_event.set()
                else:
                    self.game_focused_event.clear()
                reconnect_owns = bool(
                    self.reconnect_active_event is not None
                    and self.reconnect_active_event.is_set()
                )
                # The game's login dialog is a separate top-level window (and
                # on some clients a separate process), so the normal gameplay
                # foreground check quite correctly says "not focused" while
                # reconnect is clicking it.  That must never turn into the
                # normal lost-focus shutdown: it disarmed live input between
                # selecting a channel and the final Enter confirmations.
                # Reconnect owns focus and keyboard delivery for its entire
                # sequence, including its post-login marker gate.
                if reconnect_owns:
                    self.automation_active_event.clear()
                    self._lost_since = None
                    previous = False
                    if self.stop_event.wait(self.poll_interval):
                        break
                    continue
                active = bool(input_enabled and game_focused and not reconnect_owns)
                if active:
                    self.automation_active_event.set()
                else:
                    self.automation_active_event.clear()
                    if reconnect_owns:
                        LOG.info("focus gate: automation stays paused for the auto-reconnect")
                    # One forced release at the active -> inactive edge is
                    # enough.  Repeating resets every poll could race a
                    # successful refocus/new input session.
                    if input_enabled and previous is not False:
                        reset = getattr(self.key_sender, "reset_input_session", None)
                        if callable(reset):
                            reset("focus dip")
                        else:
                            self.key_sender.release_all_keys()
                now = time.monotonic()
                if active:
                    if self._lost_since is not None:
                        # The game is foreground again: the patrol continues by
                        # itself, whether the pause lasted 0.1s or an hour.
                        LOG.info(
                            "AUTOMATION RESUMED after %.2fs unfocused",
                            now - self._lost_since,
                        )
                        self._lost_since = None
                        self._pause_reported = False
                    previous = active
                elif input_enabled:
                    # Game not focused while keyboard input is armed.  First try
                    # to restore the foreground automatically.  Patrol is
                    # PAUSED here, never ended: keys are released and the run
                    # resumes on its own once the game is focused again.  Only
                    # the operator's own Stop Patrol ends a patrol.
                    self._try_refocus()
                    if self._lost_since is None:
                        self._lost_since = now
                        self._log_foreground_snapshot()
                    elif (not self._pause_reported
                          and now - self._lost_since
                          >= self.focus_lost_grace_seconds):
                        self._pause_reported = True
                        LOG.warning(
                            "AUTOMATION PAUSED: game window has been unfocused "
                            "for %.1fs; keys are released and the patrol resumes "
                            "by itself when the game is focused again (only "
                            "Stop Patrol ends it)",
                            now - self._lost_since,
                        )
                    previous = False
                else:
                    # Keyboard input disabled (patrol stopped by the user):
                    # idle, nothing to gate.
                    previous = False
                    self._lost_since = None
                    self._pause_reported = False
                if self.stop_event.wait(self.poll_interval):
                    break
        finally:
            self.automation_active_event.clear()
            self.game_focused_event.clear()
            reset = getattr(self.key_sender, "reset_input_session", None)
            if callable(reset):
                reset("focus worker stopped")
            else:
                self.key_sender.release_all_keys()
            LOG.info("focus worker stopped")


__all__ = ["FocusWorker"]
