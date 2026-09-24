"""Character status detection and safe keyboard actions.

This module deliberately does not capture the screen.  ``StatusWorker`` consumes
the immutable frames published by ``capture_worker`` so all decisions are made
from one coherent screenshot.
"""

from __future__ import annotations

import logging
import ctypes
import json
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

import numpy as np
from PIL import Image

from hotkey_worker import SELF_INPUT_EXTRA_INFO

LOG = logging.getLogger(__name__)

# A refused key used to be a DEBUG line, so a disarmed input looked like a dead hotkey with nothing
# in the log at all (the operator's 自动重连 report).  The first refusal of a burst is logged at INFO
# and then at most one line every few seconds, carrying the number of refusals in between.
INPUT_REFUSAL_LOG_SECONDS = 5.0
INPUT_NOT_ENABLED_REASON = (
    "live input is not enabled - press 开始巡逻/Start Patrol to arm it "
    "(typing hotkeys re-arm it themselves)"
)

# Action taps (jump / buff / periodic skill keys) are the shortest events the
# assistant emits, and ``SendInput`` is global: the keystroke lands in whichever
# window owns the keyboard at that instant.  Measured in the field log, a tap is
# also the only event that can be "sent" and still do nothing in the game, so it
# is verified at both ends of the hold and retried a bounded number of times.
_ACTION_TAP_HOLD_SECONDS = 0.045
_ACTION_TAP_ATTEMPTS = 3
_ACTION_TAP_RETRY_SECONDS = 0.06
# One delivery warning per interval, so a stolen focus cannot flood the log.
_DELIVERY_WARN_INTERVAL = 2.0

# A periodic buff is tapped every interval, so a swallowed tap costs minutes.
# Nothing in the sender can prove the GAME consumed the key, but the HUD can:
# a cast almost always moves HP/MP.  After a buff tap the worker therefore
# watches the next reading and reports what actually happened - the missing
# evidence that made "the log says executed, the game does nothing"
# undiagnosable.
_BUFF_VERIFY_SECONDS = 2.5

# Keys the UI bind buttons may capture.  The ordinary Q--M letter keys and
# slash are useful skill bindings; Z remains reserved for pickup/movement.
# Alt and arrow keys stay unavailable because patrol uses them for movement.
BINDABLE_KEYS = frozenset({
    "shift", "ctrl", "space", "delete", "end",
    "pagedown", "pageup", "home", "insert",
    "q", "w", "e", "r", "t", "y", "u", "i", "o", "p",
    "a", "s", "d", "f", "g", "h", "j", "k", "l",
    "x", "c", "v", "b", "n", "m", "slash",
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
})


class KeySender(Protocol):
    """Small interface shared by the movement and status workers."""

    def tap(self, key: str) -> bool:
        """Tap *key*, returning True only when it was sent (or dry-run logged)."""


def _process_name(pid: int) -> str:
    """Executable name for ``pid`` ('' when it cannot be queried).

    Used only for diagnostics: a window that steals the foreground is the usual
    reason an action tap dies, and naming the process is what lets the operator
    close it.  Uses kernel32 directly so it does not depend on pywin32 helpers.
    """

    if pid <= 0:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return ""
            return str(buffer.value).rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


# The game's login UI is a separate window of a second process of the same executable (measured on
# the operator's machine: `igwUserLoginDialog`, pid 5644, over the game `冒险岛怀旧服`, pid 4288, both
# Maplestory_Classic.exe).  It is the game's own input surface, so it counts as "the game is focused".
_LOGIN_UI_TITLE_HINTS = ("igwuserlogindialog",)


def _process_image_name(pid: int) -> str:
    """The executable name of a process, or "" when it cannot be read."""

    if not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return str(buffer.value).rsplit("\\", 1)[-1].casefold()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        LOG.debug("could not read the image name of pid %s", pid, exc_info=True)
    return ""


class WindowKeySender:
    """Send scan-code input only while the dynamically found game window is active."""
    # Set-1 keyboard scan codes. Extended keys require the E0 flag.
    _SCAN = {
        "ctrl": (0x1D, False), "alt": (0x38, False),
        "left": (0x4B, True), "up": (0x48, True),
        "right": (0x4D, True), "down": (0x50, True),
        "delete": (0x53, True), "end": (0x4F, True),
        "z": (0x2C, False), "space": (0x39, False),
        # digit row (potion keys)
        "1": (0x02, False), "2": (0x03, False), "3": (0x04, False),
        "4": (0x05, False), "5": (0x06, False), "6": (0x07, False),
        "7": (0x08, False), "8": (0x09, False), "9": (0x0A, False),
        "0": (0x0B, False),
        # letters
        "q": (0x10, False), "w": (0x11, False), "e": (0x12, False),
        "r": (0x13, False), "t": (0x14, False), "y": (0x15, False),
        "u": (0x16, False), "i": (0x17, False), "o": (0x18, False),
        "p": (0x19, False),
        "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False),
        "f": (0x21, False), "g": (0x22, False), "h": (0x23, False),
        "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
        "x": (0x2D, False), "c": (0x2E, False), "v": (0x2F, False),
        "b": (0x30, False), "n": (0x31, False), "m": (0x32, False),
        # function row
        "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False),
        "f4": (0x3E, False), "f5": (0x3F, False), "f6": (0x40, False),
        "f7": (0x41, False), "f8": (0x42, False), "f9": (0x43, False),
        "f10": (0x44, False), "f11": (0x57, False), "f12": (0x58, False),
        # modifiers + editing / navigation
        "shift": (0x2A, False), "tab": (0x0F, False),
        "esc": (0x01, False),
        "caps": (0x3A, False), "enter": (0x1C, False),
        "backspace": (0x0E, False),
        "home": (0x47, True), "pageup": (0x49, True),
        "pagedown": (0x51, True), "insert": (0x52, True),
        # punctuation row
        "minus": (0x0C, False), "equal": (0x0D, False),
        "bracketleft": (0x1A, False), "bracketright": (0x1B, False),
        "backslash": (0x2B, False), "semicolon": (0x27, False),
        "apostrophe": (0x28, False), "grave": (0x29, False),
        "comma": (0x33, False), "period": (0x34, False),
        "slash": (0x35, False),
        # numpad
        "kp_0": (0x52, True), "kp_1": (0x4F, True), "kp_2": (0x50, True),
        "kp_3": (0x51, True), "kp_4": (0x4B, True), "kp_5": (0x4C, True),
        "kp_6": (0x4D, True), "kp_7": (0x47, True), "kp_8": (0x48, True),
        "kp_9": (0x49, True),
        "kp_add": (0x4E, False), "kp_subtract": (0x4A, False),
        "kp_multiply": (0x37, False), "kp_divide": (0x35, True),
        "kp_enter": (0x1C, True), "kp_decimal": (0x53, True),
    }
    # MapleStory treats these as mutually exclusive motion directions.  They
    # must never be left down together: Left+Right (or Up+Down) pins the
    # character in place.  Alt and Z deliberately stay outside this set so a
    # jump chord and pickup can overlap their intended companion inputs.
    _DIRECTION_KEYS = frozenset({"left", "right", "up", "down"})
    _MOVEMENT_KEYS = ("left", "right", "up", "down", "alt", "z")

    def __init__(
        self,
        window_title: str,
        dry_run: bool = True,
        *,
        input_enabled: bool = True,
        alt_transition: bool = True,
    ) -> None:
        self.window_title = window_title
        self.dry_run = dry_run
        self.targets_configured_window = True
        # The Alt foreground-activation fallback presses the Alt key, which
        # is the JUMP key in MapleStory - a game window would jump every time
        # it is selected.  Callers for this game disable it (alt_transition
        # False) and rely on direct SetForegroundWindow + AttachThreadInput.
        self.alt_transition = bool(alt_transition)
        # Window selection is serialized, but key holds are deliberately not:
        # movement and attack workers must be able to overlap their events.
        self._selection_lock = threading.Lock()
        self._key_state_lock = threading.Lock()
        # Stop Patrol must never leave the dashboard in its "releasing keys"
        # state merely because another sender currently owns the ledger lock.
        # This guard permits one deferred bookkeeping pass after the immediate
        # game-side emergency release below.
        self._deferred_forget_lock = threading.Lock()
        self._key_owners: dict[str, int] = {}
        # v0411: the name of the caller that owns the keyboard exclusively (the auto-reconnect takes
        # it while it runs, so the attack/jump/channel-switch workers cannot type into the game).
        self._exclusive_owner: str = ""
        # SendInput can be accepted by Windows while the game misses one
        # transition.  Keep a separate best-effort physical ledger so a
        # lifecycle scrub can re-send key-up even after the logical owner
        # table was cleared by Stop Patrol or a focus dip.
        self._physical_keys: set[str] = set()
        # Every key this process has EVER injected a key-down for.  A key-up
        # can be delivered to another window when focus is stolen between the
        # two events (SendInput is global), and the game then keeps that key
        # held - after which every later tap of the same key is swallowed,
        # because the game sees a repeat instead of a new press.  A lifecycle
        # scrub therefore releases this superset, not just the movement keys.
        self._used_keys: set[str] = set()
        self._input_session = 0
        self._delivery_warned_at = float("-inf")
        # Count of assistant-sent transactions that involve Ctrl (the quick
        # message's Enter/Ctrl+V/Enter, a trade paste, or a Ctrl attack key).
        # The hotkey worker re-sends a Ctrl key-up while the operator holds Ctrl
        # after a chord; a Ctrl key-up landing inside one of OUR Ctrl chords
        # would turn that chord into nothing (the game would receive a bare
        # "v"), so those callers stand down while this is non-zero.
        self._ctrl_chord_in_flight = 0
        self._input_enabled = threading.Event()
        # Refusal-burst bookkeeping for ``_note_input_refused``.
        self._input_refusal_reason = ""
        self._input_refusal_logged_at = float("-inf")
        self._input_refusal_count = 0
        if input_enabled:
            self._input_enabled.set()
        self.hwnd: Optional[int] = None

    def enable_input(self) -> None:
        """Allow workers to emit keyboard events after explicit UI activation."""

        # Start from a neutral game-side keyboard state.  This is intentionally
        # before arming new inputs, so an old worker cannot carry a missed key
        # release into the new patrol session.
        self.reset_input_session("start patrol")
        self._input_enabled.set()
        # A fresh session reports its first refusal again instead of staying silent inside the burst
        # window of the previous one.
        self._input_refusal_reason = ""
        self._input_refusal_count = 0
        self._input_refusal_logged_at = float("-inf")
        LOG.info("live keyboard input enabled")

    def disable_input(self, *, refocus_before_release: bool = False) -> None:
        """Block input and release every assistant key.

        A Tk button click makes the assistant window foreground before its
        callback runs.  ``SendInput`` key-up events sent at that point can be
        delivered to Tk instead of MapleStory, leaving the game-side walk key
        stuck.  Stop Patrol therefore disarms workers first, then may restore
        the game window solely to deliver the neutralising key-up sequence.
        """

        # Remember whether the game already owns focus. A Ctrl+` stop happens
        # in the game window, so its immediate scrub reaches MapleStory. Doing
        # a second delayed scrub after an unnecessary refocus can land *after*
        # the operator begins holding Left/Right manually and cancel that
        # physical key-down.
        game_was_foreground = False
        if refocus_before_release and not self.dry_run:
            try:
                game_was_foreground = self.is_game_foreground()
            except Exception:
                LOG.debug("could not check game focus before input shutdown", exc_info=True)

        # Disarm before any foreground work: no worker may acquire a new key
        # while shutdown is in progress.  Do not take the ownership lock before
        # a UI-button stop has restored the game window: that old local reset
        # both sent its key-ups to Tk (not the game) and could block Stop Patrol
        # indefinitely behind a stalled sender.
        self._input_enabled.clear()
        delivered_to_game = game_was_foreground
        if refocus_before_release and not self.dry_run and not game_was_foreground:
            try:
                delivered_to_game = bool(self.select_window()) and self.is_game_foreground()
                if not delivered_to_game:
                    LOG.warning("INPUT RESET: game was not foreground after refocus")
            except Exception:
                # The game-side key-up cannot be delivered without focus. A
                # later Start Patrol always performs its own neutral reset.
                LOG.warning(
                    "INPUT RESET: game refocus failed before key release",
                    exc_info=True,
                )

        if delivered_to_game:
            # Ctrl+` is pressed in MapleStory itself. Do not wait behind a
            # worker's ownership lock before releasing the bot's keys: that
            # late release can otherwise cancel the player's manual Left/Right
            # press seconds after Stop Patrol. This same raw release is also
            # correct for a UI-button stop after it has refocused the game.
            # The ledger is cleared later without another game-side event.
            release_reason = (
                "input disabled (hotkey)"
                if game_was_foreground else "input disabled (after UI refocus)"
            )
            self._emergency_release_without_lock(release_reason)
            self._forget_input_session_or_defer(
                f"{release_reason} bookkeeping"
            )
        else:
            # No foreground game window means a raw SendInput key-up would hit
            # some other app. Forget locally without delaying the UI; Start
            # Patrol will do a full neutral game-side scrub after it selects
            # the game again.
            self._forget_input_session_or_defer("input disabled (no game focus)")
        LOG.info("live keyboard input disabled")

    def _emergency_release_without_lock(self, reason: str) -> None:
        """Send neutral key-ups now, without waiting for ownership bookkeeping.

        This is used only when the game is already foreground at an explicit
        hotkey stop. It deliberately does not edit the ledger; a concurrent
        worker may be inside its own transition. ``_forget_input_session``
        performs that local cleanup once the lock is available, without
        emitting a delayed second key-up.
        """

        keys = set(self._MOVEMENT_KEYS)
        try:
            keys.update(tuple(self._used_keys))
            keys.update(tuple(self._key_owners))
            keys.update(tuple(self._physical_keys))
        except RuntimeError:
            # A concurrent set mutation only means this emergency pass uses
            # the guaranteed movement superset; the normal ledger cleanup
            # immediately follows.
            pass
        for key in sorted(keys):
            if key not in self._SCAN:
                continue
            try:
                scan_code, extended = self._SCAN[key]
                if not self.dry_run:
                    self._send_scan_code(scan_code, key_up=True, extended=extended)
            except Exception:
                LOG.debug("emergency key release failed: %s", key, exc_info=True)
        LOG.info("INPUT EMERGENCY RELEASE reason=%s keys=%s", reason,
                 ", ".join(sorted(keys)))

    def _forget_input_session(self, reason: str) -> int:
        """Clear ownership locally without emitting any Windows key event."""

        with self._key_state_lock:
            session = self._forget_input_session_locked()
        LOG.info("INPUT SESSION FORGOTTEN session=%d reason=%s", session, reason)
        return session

    def _forget_input_session_locked(self) -> int:
        """Forget ownership while ``_key_state_lock`` is already held."""

        self._key_owners.clear()
        self._physical_keys.clear()
        self._input_session += 1
        return self._input_session

    def _forget_input_session_or_defer(self, reason: str) -> None:
        """Forget an emergency-stop ledger without making Stop Patrol wait.

        The immediate raw key-up has already reached the foreground game.  The
        remaining work is only local bookkeeping, so it is safe to let it wait
        for an in-flight sender.  In particular, holding this UI transition
        behind the ledger lock made the app appear permanently broken after a
        rope-climb transaction stalled.
        """

        if self._key_state_lock.acquire(blocking=False):
            try:
                session = self._forget_input_session_locked()
            finally:
                self._key_state_lock.release()
            LOG.info("INPUT SESSION FORGOTTEN session=%d reason=%s", session, reason)
            return

        if not self._deferred_forget_lock.acquire(blocking=False):
            LOG.info("INPUT SESSION FORGET already pending reason=%s", reason)
            return

        LOG.warning(
            "INPUT SESSION FORGET deferred: keyboard ledger is busy; "
            "Stop Patrol may continue now"
        )

        def forget_when_safe() -> None:
            try:
                with self._key_state_lock:
                    # If a new patrol was armed before this old cleanup got
                    # the lock, its fresh reset owns the ledger. Never erase
                    # that new session from an old Stop Patrol request.
                    if self._input_enabled.is_set():
                        LOG.info(
                            "INPUT SESSION FORGET skipped: a newer patrol "
                            "session is already enabled"
                        )
                        return
                    session = self._forget_input_session_locked()
                LOG.info(
                    "INPUT SESSION FORGOTTEN session=%d reason=%s (deferred)",
                    session,
                    reason,
                )
            finally:
                self._deferred_forget_lock.release()

        threading.Thread(
            target=forget_when_safe,
            name="input-session-forget",
            daemon=True,
        ).start()

    def _emit_locked(self, key: str, *, key_up: bool) -> None:
        """Emit and ledger one transition while the input state is locked."""

        if not self.dry_run:
            scan_code, extended = self._SCAN[key]
            self._send_scan_code(scan_code, key_up=key_up, extended=extended)
        if key_up:
            self._physical_keys.discard(key)
        else:
            self._physical_keys.add(key)
            self._used_keys.add(key)

    def ctrl_chord_in_flight(self) -> bool:
        """True while the assistant itself is sending a Ctrl-involving chord.

        The hotkey Ctrl cleanup re-sends a Ctrl key-up while the operator keeps
        Ctrl held after a chord.  A quick message is Enter + Ctrl+V + Enter on
        the game window, so a stray Ctrl key-up inside it would paste nothing -
        the game would receive a bare "v".  Callers that re-send synthetic
        key-ups must check this and stand down.
        """

        return self._ctrl_chord_in_flight > 0

    def _begin_ctrl_chord(self) -> None:
        self._ctrl_chord_in_flight += 1

    def _end_ctrl_chord(self) -> None:
        self._ctrl_chord_in_flight = max(0, self._ctrl_chord_in_flight - 1)

    def force_key_up(
        self, key: str, *, reason: str = "recovery", quiet: bool = False
    ) -> bool:
        """Unconditionally inject key-up and forget every claim for ``key``.

        This is the recovery path for a game-side key that may still be held
        after a lost transition.  It intentionally works when the logical
        owner count is already zero.

        ``quiet`` suppresses the per-call log line: the hotkey chord cleanup
        repeats this while the operator keeps the modifier held (see
        ``HotkeyWorker``), and one line per repeat would bury the running log.
        """

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        with self._key_state_lock:
            self._key_owners.pop(key, None)
            self._emit_locked(key, key_up=True)
        if not quiet:
            LOG.info("key-up forced=%s reason=%s", key, reason)
        return True

    def reset_input_session(self, reason: str = "reset") -> int:
        """Create a neutral input generation and scrub all bot motion keys."""

        with self._key_state_lock:
            keys = set(self._key_owners) | self._physical_keys
            # Always include the bot's movement keys.  A missed game-side
            # key-up is precisely the situation where neither local table
            # can prove the key remains down.
            keys.update(self._MOVEMENT_KEYS)
            # ... and every key this process has ever pressed: the action keys
            # (alt/ctrl and the periodic buffs) are exactly the ones whose
            # key-up is lost when focus is stolen mid-tap, and a buff key the
            # game still holds makes every later buff tap a repeat the game
            # ignores.
            keys.update(self._used_keys)
            self._key_owners.clear()
            for key in sorted(keys):
                self._emit_locked(key, key_up=True)
            self._input_session += 1
            session = self._input_session
        LOG.info(
            "INPUT RESET session=%d reason=%s forced_keys=%s",
            session, reason, ", ".join(sorted(keys)),
        )
        return session

    def release_all_keys(self, *, reason: str = "release all") -> None:
        """Release and forget all assistant keys without changing input state."""

        self.reset_input_session(reason)

    def input_session(self) -> int:
        """Monotonic generation used by workers to discard stale local holds."""

        with self._key_state_lock:
            return self._input_session

    def input_is_enabled(self) -> bool:
        return self._input_enabled.is_set()

    # ---------------------------------------------------------------- ownership (v0411)
    # Keys that must never stay down: Alt is the game's JUMP key here, and a held Alt turns a later
    # key into a Windows shell chord (Alt+Esc switches window - "a folder steals the focus" - and
    # Alt+F4 closes it).
    _MODIFIER_KEYS = ("alt", "ctrl", "shift")
    _MODIFIER_VK = ((0x12, "alt"), (0x11, "ctrl"), (0x10, "shift"))

    def begin_exclusive(self, owner: str) -> bool:
        """Give ``owner`` the keyboard: every other caller is refused until ``end_exclusive``.

        The reconnect has to arm live input for its own keys, and that same switch wakes the attack,
        random-jump and channel-switch workers - which then typed into the login and select pages
        (measured 19:07: `attack repetition: a` through the whole sequence).
        """

        with self._key_state_lock:
            previous = self._exclusive_owner
            self._exclusive_owner = str(owner or "")
        LOG.info("keyboard ownership: %r takes the keyboard (was %r)", owner, previous)
        return previous in (None, "")

    def end_exclusive(self, owner: str) -> None:
        """Release the keyboard ownership taken by ``begin_exclusive``."""

        with self._key_state_lock:
            if self._exclusive_owner == str(owner or ""):
                self._exclusive_owner = None
        LOG.info("keyboard ownership: %r released the keyboard", owner)

    def _exclusive_blocks(self, owner: str) -> bool:
        """Whether a caller must be refused because somebody else owns the keyboard."""

        current = self._exclusive_owner
        return bool(current) and str(owner or "") != current

    def release_modifiers(self, *, owner: str = "", reason: str = "auto reconnect") -> list:
        """Force Alt/Ctrl/Shift up - both our own ledger and what Windows reports.

        Returns the keys that were released.  This is what stops a stuck jump-Alt from turning the
        next Escape into Alt+Esc or the next F4 into Alt+F4.
        """

        released = []
        with self._key_state_lock:
            for key in self._MODIFIER_KEYS:
                if key in self._physical_keys or key in self._key_owners:
                    self._key_owners.pop(key, None)
                    try:
                        self._emit_locked(key, key_up=True)
                    except Exception:
                        LOG.debug("could not release %s", key, exc_info=True)
                    released.append(key)
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for vk, key in self._MODIFIER_VK:
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    scan_code, extended = self._SCAN[key]
                    self._send_scan_code(scan_code, key_up=True, extended=extended)
                    if key not in released:
                        released.append(key)
        except Exception:
            LOG.debug("could not read the live modifier state", exc_info=True)
        if released:
            LOG.warning("modifiers released by %s (%s): %s", reason, owner or "automation",
                        ", ".join(sorted(set(released))))
        return sorted(set(released))

    def _find_target_window(self, *, required: bool = True) -> int:
        """Find the configured game window without blocking the UI forever.

        ``required=False`` answers 0 instead of raising when the game is not on
        screen: callers that only want to know whether the game is there (the quick
        pickup hotkey while the operator tests a video, for example) must not turn
        that into an error.
        """
        import win32gui

        # The configured MapleStory title is normally exact. FindWindowW reads
        # it from Windows directly and avoids enumerating every top-level
        # window, which can stall when a third-party/protected window is busy.
        try:
            exact = win32gui.FindWindow(None, self.window_title)
            if exact and win32gui.IsWindowVisible(exact):
                self.hwnd = exact
                LOG.info("WINDOW SELECT: found exact-title hwnd=%s", exact)
                return exact
        except Exception:
            LOG.debug("WINDOW SELECT: exact-title lookup failed", exc_info=True)

        # Keep substring matching for users whose client adds text to the
        # title, but never allow that fallback enumeration to freeze Tk's
        # Start Patrol callback. If a foreign window blocks enumeration, the
        # temporary daemon may finish later but it cannot hold this selection.
        visible: "list[tuple[int, str]]" = []
        scan_error: list[BaseException] = []
        scan_done = threading.Event()

        def collect(hwnd: int, _extra: object) -> None:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
            except Exception:
                LOG.debug("WINDOW SELECT: skipped unreadable hwnd=%s", hwnd,
                          exc_info=True)
                return
            visible.append((hwnd, title))

        def scan() -> None:
            try:
                win32gui.EnumWindows(collect, None)
            except BaseException as exc:
                scan_error.append(exc)
            finally:
                scan_done.set()

        threading.Thread(target=scan, name="window-title-scan", daemon=True).start()
        if not scan_done.wait(1.5):
            raise OSError(
                "game-window title scan timed out; close blocking overlays or "
                "configure the exact game window title"
            )
        if scan_error:
            raise OSError("could not enumerate visible Windows windows") from scan_error[0]
        wanted = self.window_title.casefold()
        matches = [hwnd for hwnd, title in visible if wanted in title.casefold()]
        LOG.info("WINDOW SELECT: fallback title scan found %d matching window(s)",
                 len(matches))
        if len(matches) == 1:
            self.hwnd = matches[0]
            return matches[0]
        if not required:
            return 0
        raise OSError(
            (f"expected exactly one visible game window containing "
             f"{self.window_title!r}; found {len(matches)}")
            + (": " + ", ".join(
                f"{hwnd}:{title!r}" for hwnd, title in visible
                if wanted in title.casefold()
            ) if matches else "")
            + " - visible window titles: "
            + ", ".join(f"{title!r}" for _hwnd, title in visible if title.strip())[:400]
            + f"; start the game, or set its window title in the assistant (it is "
            f"currently {self.window_title!r})"
        )

    def game_window_present(self) -> bool:
        """True when the configured game window is on screen - never raises.

        Callers that only want to know whether game input is possible at all (the
        quick pickup hotkey while the operator tests a 测试测谎 video, say) must be
        able to ask without turning "the game is not running" into an error.  The
        answer is about the screen, so it is answered even in a dry run.
        """

        try:
            return self._find_target_window(required=False) != 0
        except Exception:
            LOG.debug("game window probe failed", exc_info=True)
            return False

    def select_window(self) -> bool:
        """Restore and foreground the configured game window automatically.

        The handle from the last successful selection is the least disruptive
        target, so always try it first.  If Windows cannot activate it (or the
        game recreated the top-level window), discard that handle and locate a
        fresh matching game window before trying again.  Every caller shares
        this rule: patrol, recording, trade, reconnect, and the hotkey workers
        therefore cannot keep sending input to a stale game instance.
        """

        if self.dry_run:
            return True
        import win32api
        import win32con
        import win32gui
        import win32process

        LOG.info("WINDOW SELECT: waiting for selection lock")
        with self._selection_lock:
            LOG.info("WINDOW SELECT: selection lock acquired")
            def activate(hwnd: int) -> bool:
                # Bring the game to the foreground WITHOUT pressing Alt (Alt is
                # the game's JUMP key).  Windows can refuse briefly (foreground
                # lock, or the assistant runs at a different privilege than the
                # game), so retry direct SetForegroundWindow + thread-input
                # attachment a few times before considering this handle stale.
                for attempt in range(5):
                    try:
                        if win32gui.IsIconic(hwnd):
                            # ShowWindow sends a synchronous message to the game
                            # and can freeze Tk while the game thread is busy.
                            win32gui.ShowWindowAsync(hwnd, win32con.SW_RESTORE)
                            time.sleep(0.05)
                        try:
                            win32gui.SetForegroundWindow(hwnd)
                        except Exception:
                            LOG.debug("direct foreground selection was refused",
                                      exc_info=True)
                        time.sleep(0.05)
                        if win32gui.GetForegroundWindow() == hwnd:
                            LOG.info("WINDOW SELECT: activation verified hwnd=%s", hwnd)
                            return True
                    except Exception:
                        LOG.debug("foreground attempt failed", exc_info=True)

                    # Thread-input attachment fallback.  Failure to attach one
                    # thread must not abort the other attempts.
                    foreground = win32gui.GetForegroundWindow()
                    if foreground and foreground != hwnd:
                        current_tid = win32api.GetCurrentThreadId()
                        foreground_tid = win32process.GetWindowThreadProcessId(
                            foreground)[0]
                        target_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
                        attached_threads: list[int] = []
                        try:
                            for thread_id in {foreground_tid, target_tid}:
                                if thread_id and thread_id != current_tid:
                                    try:
                                        win32process.AttachThreadInput(
                                            current_tid, thread_id, True
                                        )
                                        attached_threads.append(thread_id)
                                    except Exception:
                                        LOG.debug(
                                            "could not attach input thread %s",
                                            thread_id, exc_info=True)
                            try:
                                win32gui.SetForegroundWindow(hwnd)
                            except Exception:
                                LOG.debug(
                                    "attached foreground selection was refused",
                                    exc_info=True)
                            time.sleep(0.05)
                        finally:
                            for thread_id in reversed(attached_threads):
                                try:
                                    win32process.AttachThreadInput(
                                        current_tid, thread_id, False
                                    )
                                except Exception:
                                    pass
                        if win32gui.GetForegroundWindow() == hwnd:
                            LOG.info("WINDOW SELECT: activation verified hwnd=%s", hwnd)
                            return True
                    time.sleep(0.1)
                return False

            cached = self.hwnd
            if cached:
                try:
                    current_is_valid = bool(
                        win32gui.IsWindow(cached) and win32gui.IsWindowVisible(cached)
                    )
                except Exception:
                    current_is_valid = False
                if current_is_valid:
                    LOG.info("WINDOW SELECT: trying current hwnd=%s", cached)
                    if activate(cached):
                        return True
                    LOG.warning("WINDOW SELECT: current hwnd=%s could not be activated; "
                                "re-anchoring game window", cached)
                else:
                    LOG.info("WINDOW SELECT: cached hwnd=%s is stale; re-anchoring game window",
                             cached)

            # A game can recreate its top-level window while keeping the same title.
            # Only search after the current instance was proven unavailable.
            self.hwnd = None
            try:
                hwnd = self._find_target_window()
                LOG.info("WINDOW SELECT: re-anchored hwnd=%s", hwnd)
                if activate(hwnd):
                    return True
                raise OSError(
                    "Windows 拒绝将游戏窗口置为前台。请确认：1) 助手与游戏以"
                    "相同权限运行（同为管理员或同为普通用户）；2) 游戏窗口"
                    "未被最小化或遮挡。"
                )
            except Exception as exc:
                raise OSError(
                    f"could not automatically select the current game window: {exc}"
                ) from exc

    def _foreground_matches(self) -> bool:
        try:
            import win32gui
            foreground = win32gui.GetForegroundWindow()
            if foreground and win32gui.IsWindow(foreground):
                foreground_title = win32gui.GetWindowText(foreground)
                if self.window_title.casefold() in foreground_title.casefold():
                    self.hwnd = foreground
                    return True
            if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                self._find_target_window()
            return foreground == self.hwnd
        except Exception as exc:  # pywin32 absent, non-Windows, or desktop unavailable
            # 游戏窗口未找到/未启动是正常状态，不当作告警刷屏。
            LOG.debug("cannot verify foreground window: %s", exc)
            return False

    def is_target_focused(self) -> bool:
        """Public focus predicate used by movement-worker safety checks."""

        if not self.input_is_enabled():
            return False
        return self.is_game_foreground()

    def is_game_foreground(self) -> bool:
        """Check focus without enabling input or selecting any window."""

        if self.dry_run:
            return True
        return self._foreground_matches()

    def _note_input_refused(self, key: str, reason: str) -> None:
        """Say out loud why a key was refused, once per burst.

        ``key_down``/``tap`` refuse a key while live input is disarmed.  That refusal was a DEBUG
        line, so the operator saw a hotkey that "did nothing" and a log that said nothing.
        """

        now = time.monotonic()
        if (reason == self._input_refusal_reason
                and now - self._input_refusal_logged_at < INPUT_REFUSAL_LOG_SECONDS):
            self._input_refusal_count += 1
            return
        extra = ""
        if reason == self._input_refusal_reason and self._input_refusal_count:
            extra = f" ({self._input_refusal_count} more refused meanwhile)"
        self._input_refusal_reason = reason
        self._input_refusal_count = 0
        self._input_refusal_logged_at = now
        LOG.info("key send refused: key=%s - %s%s", key, reason, extra)

    def press(self, key: str, duration: float = 0.025, *, owner: str = "") -> bool:
        """Press a key using native SendInput scan-code keyboard events only.

        ``owner`` is the caller's name: while another owner holds the keyboard exclusively (the
        auto-reconnect), a call from anyone else is refused.
        """

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        if self._exclusive_blocks(owner):
            LOG.info("blocked key=%s: the keyboard is owned by %r", key, self._exclusive_owner)
            return False
        # Movement holds may intentionally last multiple seconds.  The former
        # 0.5-second upper clamp silently shortened a requested 2-second hold.
        duration = float(np.clip(duration, 0.01, 10.0))
        started = time.monotonic()
        claimed = False
        try:
            # The owner MUST be carried through: press() already passed the ownership check, and a
            # bare key_down() here would be refused by the very rule press() just satisfied.
            claimed = self.key_down(key, owner=owner)
            if not claimed:
                return False
            # One uninterrupted hold. Repeated direction transitions make the
            # game restart movement in tiny steps instead of walking smoothly.
            time.sleep(duration)
            return True
        except Exception:
            LOG.exception("failed to send key=%s", key)
            return False
        finally:
            if claimed:
                self.key_up(key, owner=owner)
                LOG.info("key hold complete=%s actual_hold=%.3fs", key,
                         time.monotonic() - started)

    def key_down(self, key: str, *, owner: str = "") -> bool:
        """Claim a key; inject key-down only for the first concurrent owner."""

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        if self._exclusive_blocks(owner):
            LOG.info("blocked key-down=%s: the keyboard is owned by %r", key,
                     self._exclusive_owner)
            return False
        if not self.input_is_enabled():
            self._note_input_refused(key, INPUT_NOT_ENABLED_REASON)
            return False
        if self.dry_run:
            LOG.info("DRY-RUN key-down=%s target=%r", key, self.window_title)
        elif not self._foreground_matches():
            LOG.debug("blocked key-down=%s: game window is not foreground", key)
            return False
        with self._key_state_lock:
            # Stop Patrol clears this event before acquiring the same lock for
            # its forced releases.  Check again under the lock so a queued
            # worker cannot inject a late key-down after Stop was clicked.
            if not self.input_is_enabled():
                LOG.debug("blocked key-down=%s: input disarmed during wait", key)
                return False
            if key in self._DIRECTION_KEYS:
                # Direction transitions are serialized centrally.  Release
                # every conflicting direction BEFORE pressing this one; Z and
                # Alt are intentionally unaffected and may still overlap.
                for other in self._DIRECTION_KEYS - {key}:
                    if (other in self._key_owners
                            or other in self._physical_keys):
                        self._key_owners.pop(other, None)
                        self._emit_locked(other, key_up=True)
                        LOG.info("key-up forced=%s reason=direction-switch", other)
            owners = self._key_owners.get(key, 0)
            if owners == 0:
                self._emit_locked(key, key_up=False)
            self._key_owners[key] = owners + 1
        LOG.info("key-down=%s owners=%d", key, owners + 1)
        return True

    def key_up(self, key: str, *, owner: str = "") -> bool:
        """Release one claim; inject key-up only after the final owner exits.

        A key-up is NEVER blocked by ownership: releasing is always safe, and blocking it would leave
        a key stuck down.
        """

        key = key.casefold()
        with self._key_state_lock:
            owners = self._key_owners.get(key, 0)
            if owners <= 0:
                return False
            remaining = owners - 1
            if remaining:
                self._key_owners[key] = remaining
            else:
                self._key_owners.pop(key, None)
                self._emit_locked(key, key_up=True)
        LOG.info("key-up=%s owners=%d", key, remaining)
        return True

    def repeat_key_down(self, key: str) -> bool:
        """Re-emit key-down for an already-owned movement key.

        This does not acquire another ownership reference and therefore does
        not affect when the final key-up is sent.
        """

        key = key.casefold()
        if key not in ("left", "right"):
            raise ValueError("repeat_key_down is only for movement directions")
        if not self.input_is_enabled():
            return False
        if self.dry_run:
            LOG.info("DRY-RUN repeat key-down=%s", key)
            return True
        if not self._foreground_matches():
            return False
        with self._key_state_lock:
            if (not self.input_is_enabled()
                    or self._key_owners.get(key, 0) <= 0):
                return False
            self._emit_locked(key, key_up=False)
        return True

    def is_key_down(self, key: str) -> bool:
        """Return whether any worker currently owns the key."""

        with self._key_state_lock:
            return self._key_owners.get(key.casefold(), 0) > 0

    def tap(self, key: str, *, owner: str = "") -> bool:
        """Tap an ACTION key (jump/buff) and verify the game got it.

        ``owner`` follows the same rule as :meth:`press`: while another owner holds the keyboard
        exclusively (the auto-reconnect), a tap from anyone else is refused.

        A tap is the shortest event this process emits (25-45 ms), so it is the
        one that loses the race with a window that steals the foreground:
        ``SendInput`` is global, so the keystroke is delivered to whatever owns
        the keyboard at that instant.  The old implementation called
        ``press()`` once and reported success whenever ``SendInput`` returned,
        which is why the log showed ``motion arbiter executed …`` while nothing
        happened in the game.

        This tap instead:
        - refuses/retries while the game is not foreground,
        - re-checks focus at the END of the hold (a key-up delivered to another
          window leaves the key stuck down in the game, which makes every later
          tap of that key a no-op),
        - force-releases the key when that happens,
        - logs a rate-limited WARNING naming the window that stole the focus.

        Returns True only when the game owned the keyboard for the whole tap.
        """

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        if self._exclusive_blocks(owner):
            LOG.info("blocked tap=%s: the keyboard is owned by %r", key, self._exclusive_owner)
            return False
        for attempt in range(1, _ACTION_TAP_ATTEMPTS + 1):
            if not self.input_is_enabled():
                # Not a transient condition: no retry, no delay.
                self._note_input_refused(key, INPUT_NOT_ENABLED_REASON)
                return False
            if not (self.dry_run or self._foreground_matches()):
                self._warn_delivery(
                    "action tap %s refused: game window is not foreground (%s)",
                    key, self.describe_foreground(),
                )
                if attempt < _ACTION_TAP_ATTEMPTS:
                    time.sleep(_ACTION_TAP_RETRY_SECONDS)
                continue
            if self._tap_verified(key, owner=owner):
                return True
            if attempt < _ACTION_TAP_ATTEMPTS:
                time.sleep(_ACTION_TAP_RETRY_SECONDS)
        LOG.warning(
            "action tap %s NOT delivered after %d attempts (foreground=%s)",
            key, _ACTION_TAP_ATTEMPTS, self.describe_foreground(),
        )
        return False

    def _tap_verified(self, key: str, *, owner: str = "") -> bool:
        """One down/hold/up with a focus check at both ends (see :meth:`tap`)."""

        started = time.monotonic()
        # The configured attack key may be Ctrl itself: the hotkey cleanup's
        # synthetic Ctrl key-up must not land inside our own attack tap, so the
        # whole down/hold/up transaction stands down while it runs.
        own_ctrl = key == "ctrl"
        if own_ctrl:
            self._begin_ctrl_chord()
        try:
            return self._tap_verified_inner(key, owner=owner, started=started)
        finally:
            if own_ctrl:
                self._end_ctrl_chord()

    def _tap_verified_inner(
        self, key: str, *, owner: str, started: float
    ) -> bool:
        if not self.key_down(key, owner=owner):
            return False
        owned_at_release = True
        try:
            time.sleep(_ACTION_TAP_HOLD_SECONDS)
            # The release must land in the game too: a key-up that goes to a
            # window which stole the focus leaves the game holding the key.
            owned_at_release = bool(self.dry_run or self._foreground_matches())
        finally:
            self.key_up(key, owner=owner)
            LOG.info("key hold complete=%s actual_hold=%.3fs", key,
                     time.monotonic() - started)
        if not owned_at_release:
            self.force_key_up(key, reason="action tap lost focus")
            self._warn_delivery(
                "action tap %s lost the game window mid-tap (%s); key "
                "force-released and retried",
                key, self.describe_foreground(),
            )
            return False
        return True

    def _warn_delivery(self, message: str, *args: Any) -> None:
        """Rate-limited delivery warning (one line per 2 s, never per attempt)."""

        now = time.monotonic()
        if now - self._delivery_warned_at < _DELIVERY_WARN_INTERVAL:
            LOG.debug(message, *args)
            return
        self._delivery_warned_at = now
        LOG.warning(message, *args)

    @staticmethod
    def describe_foreground() -> str:
        """Human-readable description of the current foreground window.

        A stolen focus is the usual reason an action tap dies silently, so the
        warning has to name the thief: title, class, hwnd, pid and, when the
        process can be queried, its executable name.
        """

        try:
            import win32gui

            hwnd = int(win32gui.GetForegroundWindow() or 0)
            if not hwnd:
                return "foreground=<none>"
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            pid = 0
            try:
                import win32process

                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            exe = _process_name(int(pid))
            return (
                f"hwnd={hwnd} title={title[:40]!r} class={class_name!r} "
                f"pid={pid}{f' exe={exe}' if exe else ''}"
            )
        except Exception as exc:
            return f"foreground=<unavailable: {exc}>"

    def send_clipboard_message(self) -> bool:
        """Explicit UI action: focus game and send Enter, Ctrl+V, Enter.

        This deliberately works while patrol input is disarmed. It never
        types arbitrary text itself—the UI has already placed the chosen
        quick message on the Windows clipboard. Holding the key-state lock
        prevents movement/attack injection from interleaving with the chat
        chord when patrol happens to be active.
        """

        if self.select_window() is False or not self.is_game_foreground():
            return False
        if self.dry_run:
            LOG.info("DRY-RUN quick message: enter, ctrl+v, enter")
            return True

        def transition(key: str, key_up: bool) -> None:
            scan_code, extended = self._SCAN[key]
            self._send_scan_code(scan_code, key_up=key_up, extended=extended)

        def direct_tap(key: str) -> None:
            transition(key, False)
            time.sleep(0.025)
            transition(key, True)

        with self._key_state_lock:
            # Release any gameplay hold before opening chat, and forget the
            # matching ownership so no later worker releases a stale claim.
            held_keys = tuple(self._key_owners)
            self._key_owners.clear()
            for key in held_keys:
                transition(key, True)
                self._physical_keys.discard(key)
            # The message interaction intentionally cancels gameplay holds.
            # Advance the generation so a persistent climb cannot assume Up
            # is still down after its chat key sequence.
            self._input_session += 1
            direct_tap("enter")
            # Older/slower clients need time to open chat before Ctrl+V and
            # to consume the clipboard paste before the final Enter.
            time.sleep(0.15)
            # The hotkey Ctrl cleanup must not re-send a Ctrl key-up inside this
            # chord: the paste would become a bare "v" in the game.
            self._begin_ctrl_chord()
            try:
                transition("ctrl", False)
                direct_tap("v")
                transition("ctrl", True)
            finally:
                self._end_ctrl_chord()
            time.sleep(0.15)
            direct_tap("enter")
        LOG.info("quick message pasted and sent to game window")
        return True

    def send_direct_keys(self, *chords: str) -> bool:
        """Send short explicit UI chords while patrol input is disarmed.

        Trade confirmation/message dialogs need Enter and Ctrl+V without the
        normal chat-opening Enter used by :meth:`send_clipboard_message`.
        This remains serialized with all assistant input and never depends on
        the patrol input-enabled gate.
        """

        normalized: list[tuple[str, ...]] = []
        for chord in chords:
            keys = tuple(part.strip().casefold() for part in str(chord).split("+") if part.strip())
            if not keys or any(key not in self._SCAN for key in keys):
                raise ValueError(f"unsupported direct key chord: {chord!r}")
            normalized.append(keys)
        if self.dry_run:
            LOG.info("DRY-RUN direct keys: %s", ", ".join(chords))
            return True

        def transition(key: str, key_up: bool) -> None:
            scan_code, extended = self._SCAN[key]
            self._send_scan_code(scan_code, key_up=key_up, extended=extended)

        with self._key_state_lock:
            # A chord of ours that contains Ctrl must not have a synthetic Ctrl
            # key-up (the hotkey cleanup) land inside it.
            involves_ctrl = any("ctrl" in keys for keys in normalized)
            if involves_ctrl:
                self._begin_ctrl_chord()
            try:
                for keys in normalized:
                    for key in keys:
                        transition(key, False)
                    time.sleep(0.025)
                    for key in reversed(keys):
                        transition(key, True)
                    time.sleep(0.08)
            finally:
                if involves_ctrl:
                    self._end_ctrl_chord()
        return True

    @staticmethod
    def _send_scan_code(scan_code: int, *, key_up: bool, extended: bool) -> None:
        """Inject one hardware-like keyboard transition with Win32 SendInput.

        Every injected event is stamped with ``SELF_INPUT_EXTRA_INFO`` in
        ``dwExtraInfo`` so ``HotkeyWorker`` can tell the assistant's own keys
        from foreign injection (Mouse Without Borders, remote desktops) and
        keep its chord state machine clean.
        """

        from ctypes import wintypes

        ULONG_PTR = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUT_UNION(ctypes.Union):
            # All members are required: the union size must match Win32 INPUT
            # (40 bytes on 64-bit Windows), even when only `ki` is used.
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT),
            ]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("union",)
            _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_SCANCODE = 0x0008
        flags = KEYEVENTF_SCANCODE
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if key_up:
            flags |= KEYEVENTF_KEYUP
        event = INPUT(type=1, ki=KEYBDINPUT(
            0, scan_code, flags, 0, SELF_INPUT_EXTRA_INFO
        ))
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        ctypes.set_last_error(0)
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, f"SendInput injected {sent}/1 events")


@dataclass(frozen=True)
class StatusReading:
    hp: Optional[int]
    mp: Optional[int]
    hp_ratio: Optional[float]
    mp_ratio: Optional[float]
    confidence: float
    exp: Optional[int] = None
    exp_ratio: Optional[float] = None


@dataclass(frozen=True)
class StatusConfig:
    """Calibration values for the classic bottom-centre HP/MP/EXP bars.

    ``status_roi`` is (left, top, right, bottom) in normalized frame units.
    The capture region is the FIXED-PIXEL bottom-middle info bar defined in
    ``assistant.py`` (``status_capture_pixel_box``); the three bars sit SIDE
    BY SIDE in the same vertical band - HP (red) left, MP (blue) middle, EXP
    (yellow) right.  Each bar is measured ONLY inside its own horizontal zone
    (``bar_zones``, fractions of the ROI width) so the three can never be
    mixed up.
    """

    status_roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    max_hp: int = 656
    max_mp: int = 371
    hp_threshold: int = 300
    mp_threshold: int = 60
    # Drug panel settings: keys bound to the HP/MP potion slots and the
    # trigger thresholds as ratios (0..1 = percent/100 of the bar).  When the
    # bar ratio drops BELOW the threshold the key is tapped (debounced by
    # low_frames_required + potion_cooldown).
    hp_key: str = "delete"
    mp_key: str = "end"
    hp_ratio_threshold: float = 0.5
    mp_ratio_threshold: float = 0.3
    hp_enabled: bool = True
    mp_enabled: bool = True
    # Periodic buff keys (the extra Drug panel rows): the bound key is
    # tapped on a TIMER (``buffN_interval`` seconds, default 10 minutes)
    # instead of a bar-ratio threshold.  Disabled or empty keys never fire.
    buff1_key: str = "home"
    buff2_key: str = "insert"
    buff3_key: str = "pageup"
    buff1_interval: float = 600.0
    buff2_interval: float = 600.0
    buff3_interval: float = 600.0
    buff1_enabled: bool = False
    buff2_enabled: bool = False
    buff3_enabled: bool = False
    # Three side-by-side bars in the fixed-pixel info bar.  Measured on the
    # current real client at the 1080x768 preset (425x32 capture): HP red
    # x 367-499, MP blue x 502-631, EXP yellow x 641-771 - each bar is ~130 px
    # wide.  Zones are (name, left, right) fractions of the ROI width so the
    # bars can never be mixed.
    bar_zones: tuple[tuple[str, float, float], ...] = (
        ("hp", 0.02, 0.34),
        ("mp", 0.34, 0.65),
        ("exp", 0.66, 0.98),
    )
    # Vertical band (top, bottom) as fractions of the ROI height: the bars
    # occupy the middle rows of the 32px capture; the band excludes the panel
    # chrome above and below them.
    bar_band: tuple[float, float] = (0.50, 0.96)
    # Full bar length per bar as a fraction of the ROI width (FIXED PIXEL
    # HUD - measured on the current real client: ~130 px per bar at the
    # 1080x768 preset, i.e. 164 px inside the 538px reference capture).
    # Accepted candidates may vary.
    full_bar_width_fractions: dict[str, float] = field(
        default_factory=lambda: {
            "hp": 164.0 / 538.0,
            "mp": 164.0 / 538.0,
            "exp": 164.0 / 538.0,
        }
    )
    min_bar_width_fraction: float = 5.0 / 538.0
    minimum_action_confidence: float = 0.55


class BarStatusDetector:
    """Find red HP and blue MP horizontal fills without OCR.

    The broad lower-middle ROI makes this resolution independent.  Confidence is
    deliberately conservative: ambiguous/missing bars produce ``None`` and no
    potion action instead of guessing.
    """

    def __init__(self, config: StatusConfig = StatusConfig()) -> None:
        self.config = config
        # Adaptive full-bar reference per bar (hp/mp): the longest plausible
        # fill run observed.  The game HUD does not always scale 1:1 with the
        # client width (some setups keep fixed-pixel bars), so a pure
        # client-fraction estimate can be smaller than the real bar - that
        # clips every ratio to 1.0 and potions would never fire.  The
        # observed full run self-calibrates to the ACTUAL bar length only once
        # it is clearly wider than the conservative initial estimate.  A
        # partially filled bar must never be adopted as "full": doing so
        # inflates every later percentage and causes a 30% potion threshold
        # to fire dangerously late.
        self._full_run: dict[str, Optional[int]] = {
            "hp": None, "mp": None, "exp": None,
        }
        self._ref_width: int = 0

    @staticmethod
    def _longest_run(mask: np.ndarray) -> tuple[int, int, int]:
        best = (0, 0, 0)  # length, row, start
        for row_number, row in enumerate(mask):
            padded = np.pad(row.astype(np.int8), (1, 1))
            edges = np.diff(padded)
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            if starts.size:
                index = int(np.argmax(ends - starts))
                candidate = (int(ends[index] - starts[index]), row_number,
                             int(starts[index]))
                if candidate[0] > best[0]:
                    best = candidate
        return best

    def _ratio(self, mask: np.ndarray, frame_width: int,
               name: str) -> tuple[Optional[float], float]:
        minimum = frame_width * self.config.min_bar_width_fraction
        expected = max(
            1.0,
            frame_width * self.config.full_bar_width_fractions.get(
                name, self.config.full_bar_width_fractions["hp"]
            ),
        )
        # Only bar-plausible runs count: the fill is at most ~1x the fraction
        # estimate when full.  A WIDER red/blue element in the ROI (HUD
        # frame, bar-track glow, character effect) would otherwise be
        # measured as the fill and lock the ratio at 1.0 - HP/MP never
        # drops and potions never fire.
        bar_max = max(expected * 2.0, 60.0)
        best = (0, -1, 0)  # (length, row, start)
        widest = 0
        for row_number, row in enumerate(mask):
            padded = np.pad(row.astype(np.int8), (1, 1))
            edges = np.diff(padded)
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            if starts.size:
                index = int(np.argmax(ends - starts))
                run_candidate = int(ends[index] - starts[index])
                widest = max(widest, run_candidate)
                if (minimum <= run_candidate <= bar_max
                        and run_candidate > best[0]):
                    best = (run_candidate, row_number, int(starts[index]))
        run, row, start = best
        if run < minimum:
            return None, 0.0
        if widest > bar_max:
            LOG.info("status bar (%s): ignored wide run %s px; using %s px",
                     name, widest, run)
        # Merge several neighbouring scanlines. Anti-aliasing/borders otherwise
        # make a one-row estimate unnecessarily fragile.
        top, bottom = max(0, row - 2), min(mask.shape[0], row + 3)
        local_runs = [
            self._longest_run(mask[y:y + 1])[0] for y in range(top, bottom)
        ]
        local_runs = [value for value in local_runs
                      if minimum <= value <= bar_max]
        if not local_runs:
            local_runs = [run]
        run = int(np.median(local_runs))
        # Window resized / different resolution: drop the old reference.
        if frame_width != self._ref_width:
            self._ref_width = frame_width
            self._full_run[name] = None
        reference = self._full_run.get(name)
        if reference is not None:
            expected = max(expected, float(reference))
        # Only a run clearly beyond the conservative estimate can establish
        # the real full-bar length.  A 75%-wide sample used to be accepted
        # here; it then made 20% actual MP appear to be 30%+ and delayed
        # potion use.  When uncertain, retaining the larger estimate is safe:
        # it may drink a little early, never late.
        if run >= expected * 1.10:
            if reference is None or run > reference:
                self._full_run[name] = run
                expected = max(expected, float(run))
                LOG.info("status bar reference adapted (%s): full run %s px",
                         name, run)
        ratio = float(np.clip(run / expected, 0.0, 1.0))
        confidence = min(1.0, run / minimum) * (0.75 if ratio >= 0.995 else 1.0)
        return ratio, confidence

    @staticmethod
    def _bar_mask(name: str, red: np.ndarray, green: np.ndarray,
                  blue: np.ndarray) -> np.ndarray:
        """Color mask for one bar: HP red, MP blue, EXP yellow.

        Each mask accepts the measured fill range (bright core to dark
        edge) and excludes the gray track, white separators and the OTHER
        two bars' colors, so the three bars can never be mixed up.
        """

        if name == "hp":
            return (red >= 60) & (red >= green * 1.6) & (red >= blue * 1.5)
        if name == "mp":
            return (blue >= 60) & (blue >= red * 1.5) & (blue >= green * 1.3)
        # EXP: yellow-green fill (bright 238,255,0 -> dark 88,102,0), low
        # blue; the gray/white track and separators have blue > 130.
        return ((green >= 60) & (blue <= 130) & (red >= green * 0.7)
                & (green >= blue * 1.2))

    def detect(self, image: Image.Image) -> StatusReading:
        width, height = image.size
        left, top, right, bottom = self.config.status_roi
        pixel_box = (
            max(0, min(width, int(left * width))),
            max(0, min(height, int(top * height))),
            max(0, min(width, int(right * width))),
            max(0, min(height, int(bottom * height))),
        )
        if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
            return StatusReading(None, None, None, None, 0.0)
        # Convert only the tiny status area instead of allocating an int16
        # NumPy copy of the entire captured client image.
        crop = np.asarray(image.crop(pixel_box).convert("RGB"), dtype=np.int16)
        if crop.size == 0:
            return StatusReading(None, None, None, None, 0.0)

        # The three bars share one vertical band but sit SIDE BY SIDE:
        # restrict to the band first (excludes blue UI text/decoration above
        # the bars), then measure each bar only inside its own horizontal
        # zone so HP/MP/EXP can never be mixed.
        crop_height, crop_width = crop.shape[0], crop.shape[1]
        band_top = int(round(self.config.bar_band[0] * crop_height))
        band_bottom = int(round(self.config.bar_band[1] * crop_height))
        band = slice(max(0, band_top), min(crop_height, band_bottom))
        red, green, blue = crop[..., 0], crop[..., 1], crop[..., 2]
        readings: dict[str, tuple[Optional[float], float]] = {}
        for name, zone_left, zone_right in self.config.bar_zones:
            zone = slice(
                int(round(zone_left * crop_width)),
                int(round(zone_right * crop_width)),
            )
            mask = self._bar_mask(
                name, red[band, zone], green[band, zone], blue[band, zone]
            )
            readings[name] = self._ratio(mask, width, name)
        hp_ratio, hp_conf = readings["hp"]
        mp_ratio, mp_conf = readings["mp"]
        exp_ratio, exp_conf = readings["exp"]
        hp = round(hp_ratio * self.config.max_hp) if hp_ratio is not None else None
        mp = round(mp_ratio * self.config.max_mp) if mp_ratio is not None else None
        exp = round(exp_ratio * 100) if exp_ratio is not None else None
        confidence = min(hp_conf, mp_conf) if hp is not None and mp is not None else 0.0
        return StatusReading(
            hp, mp, hp_ratio, mp_ratio, confidence,
            exp=exp, exp_ratio=exp_ratio,
        )


class StatusWorker(threading.Thread):
    """Monitor status frames and use potions; contains no attack logic."""

    def __init__(
        self,
        frame_queue: queue.Queue,
        key_sender: KeySender,
        stop_event: threading.Event,
        *,
        detector: Optional[BarStatusDetector] = None,
        automation_active_event: Optional[threading.Event] = None,
        potion_cooldown: float = 5.0,
        low_frames_required: int = 1,
        potion_retry_attempts: int = 3,
        potion_retry_delay_seconds: float = 0.05,
        potion_verify_seconds: float = 1.25,
        potion_verify_retries: int = 1,
        status_state_path: Optional[str] = None,
        motion_arbiter: Any = None,
    ) -> None:
        super().__init__(name="status-worker", daemon=True)
        self.frame_queue = frame_queue
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.detector = detector or BarStatusDetector()
        self.automation_active_event = automation_active_event
        # Optional MotionArbiter: periodic buff taps are queued and executed
        # in a motion-free window instead of being fired straight into
        # whatever action motion is playing.  HP/MP potions stay urgent and
        # keep their direct, bar-verified path.
        self.motion_arbiter = motion_arbiter
        self.potion_cooldown = max(0.0, potion_cooldown)
        self.low_frames_required = max(1, low_frames_required)
        # Potions are the highest-priority action: if a tap is blocked
        # (foreground flicker, momentary key ownership) it is retried a few
        # times before giving up, and the next low frame retries anyway.
        self.potion_retry_attempts = max(1, int(potion_retry_attempts))
        self.potion_retry_delay_seconds = max(
            0.0, float(potion_retry_delay_seconds)
        )
        # A successful SendInput call only proves the key was sent, not that
        # the game consumed the potion.  Check the actual coloured fill soon
        # afterwards and permit one priority retry if it did not rise.
        self.potion_verify_seconds = max(0.25, float(potion_verify_seconds))
        self.potion_verify_retries = max(0, int(potion_verify_retries))
        # Optional shared state file: latest HP/MP ratios, read by the
        # movement worker so the channel-switch safety net can gate its
        # potion on the current health.
        self.status_state_path = status_state_path
        self._low_count = {"hp": 0, "mp": 0}
        self._last_potion = {"hp": float("-inf"), "mp": float("-inf")}
        self._potion_verification: dict[str, Optional[dict[str, float | int]]] = {
            "hp": None, "mp": None,
        }
        # Monotonic timestamps of the last completed timed action (per row).
        # 增益为"定时触发"，不从开局立即触发：起始时间戳设为当前时刻，
        # 第一个增益会在 interval 秒后才按（用户会先手动触发第一次增益）。
        self._last_buff = {"buff1": time.monotonic(), "buff2": time.monotonic(),
                          "buff3": time.monotonic()}
        # Buff 2/3 can wait in MotionArbiter while climbing.  Keep one local
        # in-flight marker so an elapsed timer cannot enqueue duplicates; it
        # clears only when the arbiter reports success/failure.
        self._buff_pending = {"buff2": False, "buff3": False}
        self._buff_timer_lock = threading.Lock()
        # Buff landing verification: the HUD readings before a buff tap, so the
        # next frames can prove whether the game cast anything at all.
        self._buff_verification: dict[str, Optional[dict[str, Any]]] = {
            "buff1": None, "buff2": None, "buff3": None,
        }
        self._last_hp: Optional[int] = None
        self._last_mp: Optional[int] = None

    def _tap_potion(self, key: str) -> bool:
        """Tap the potion key, retrying briefly if the first attempt is blocked.

        Potions are the highest-priority action: a transient block (foreground
        flicker, momentary key ownership) must not leave the character unable
        to eat.  When all attempts fail the caller keeps the last-potion
        timestamp stale, so the next low frame retries again anyway.
        """

        for _ in range(self.potion_retry_attempts):
            if self.key_sender.tap(key):
                return True
            if self.potion_retry_delay_seconds > 0:
                time.sleep(self.potion_retry_delay_seconds)
        return False

    def _check_resource(self, name: str, ratio: Optional[float],
                        threshold_ratio: float, key: str, now: float) -> None:
        if ratio is None:
            self._low_count[name] = 0
            return
        self._low_count[name] = (
            self._low_count[name] + 1 if ratio < threshold_ratio else 0
        )
        if (self._low_count[name] >= self.low_frames_required
                and now - self._last_potion[name] >= self.potion_cooldown):
            if self._tap_potion(key):
                self._last_potion[name] = now
                self._low_count[name] = 0
                self._potion_verification[name] = {
                    "before_ratio": ratio,
                    "deadline": now + self.potion_verify_seconds,
                    "retries": 0,
                }
                LOG.warning("%s=%.0f%% below %.0f%%: used %s", name.upper(),
                            ratio * 100, threshold_ratio * 100, key)

    def _verify_potion_effect(
        self,
        name: str,
        ratio: Optional[float],
        threshold_ratio: float,
        key: str,
        now: float,
    ) -> bool:
        """Confirm a sent potion raised its own progress bar.

        Returns true while verification owns this resource, preventing the
        ordinary cooldown path from delaying or duplicating its retry.
        """

        pending = self._potion_verification[name]
        if pending is None:
            return False
        if ratio is not None and ratio >= float(pending["before_ratio"]) + 0.02:
            LOG.info("%s potion verified: bar rose to %.0f%%", name.upper(),
                     ratio * 100)
            self._potion_verification[name] = None
            return False
        if now < float(pending["deadline"]):
            return True
        retries = int(pending["retries"])
        if (ratio is not None and ratio < threshold_ratio
                and retries < self.potion_verify_retries
                and self._tap_potion(key)):
            self._last_potion[name] = now
            self._potion_verification[name] = {
                "before_ratio": ratio,
                "deadline": now + self.potion_verify_seconds,
                "retries": retries + 1,
            }
            LOG.warning(
                "%s potion was not reflected by its bar; priority retry %s",
                name.upper(), key,
            )
            return True
        LOG.warning("%s potion effect could not be verified", name.upper())
        self._potion_verification[name] = None
        return False

    def _arm_buff_verification(self, name: str, key: str) -> None:
        """Remember the HUD resources so the next frames can prove a cast.

        Called right after a buff key reported "sent".  The comparison baseline
        is the most recent HUD reading, so the very next reading decides it.
        """

        self._buff_verification[name] = {
            "key": str(key),
            "hp_before": self._last_hp,
            "mp_before": self._last_mp,
            "deadline": time.monotonic() + _BUFF_VERIFY_SECONDS,
        }

    def _verify_buff_effects(self, reading: Any, now: float) -> None:
        """Report whether each pending buff tap actually changed the HUD.

        A "cast detected" line means the game consumed the key; the warning
        means it did not, which is the case an operator must know about (an
        in-game key binding that no longer matches the configured buff key, or
        a key the client refuses to consume).
        """

        for name, pending in self._buff_verification.items():
            if pending is None:
                continue
            hp_now, mp_now = getattr(reading, "hp", None), getattr(reading, "mp", None)
            hp_before, mp_before = pending["hp_before"], pending["mp_before"]
            readable = hp_now is not None or mp_now is not None
            changed = (
                (hp_before is not None and hp_now is not None and hp_now != hp_before)
                or (mp_before is not None and mp_now is not None and mp_now != mp_before)
            )
            if changed:
                LOG.info(
                    "%s %s: cast detected (HP %s -> %s, MP %s -> %s)",
                    name.upper(), pending["key"], hp_before, hp_now,
                    mp_before, mp_now,
                )
                self._buff_verification[name] = None
                continue
            if now < float(pending["deadline"]):
                continue
            self._buff_verification[name] = None
            if not readable:
                LOG.warning(
                    "%s %s: could not verify the cast (HP/MP unreadable)",
                    name.upper(), pending["key"],
                )
                continue
            LOG.warning(
                "%s %s: no HP/MP change within %.1fs after the tap - the game "
                "did not cast anything (check that the in-game skill is bound "
                "to %s, and that the window really had focus)",
                name.upper(), pending["key"], _BUFF_VERIFY_SECONDS,
                pending["key"],
            )

    def _check_buffs(self, now: float) -> None:
        """Tap the periodic buff keys when their timer elapses.

        Time-based (unlike the HP/MP potions), so this runs before the
        bar-confidence gate: the bound key is sent every ``buffN_interval``
        seconds while automation is active (the run loop already gates on the
        automation event).
        """

        config = self.detector.config
        for name, key, interval, enabled in (
            ("buff1", config.buff1_key, config.buff1_interval,
             config.buff1_enabled),
            ("buff2", config.buff2_key, config.buff2_interval,
             config.buff2_enabled),
            ("buff3", config.buff3_key, config.buff3_interval,
             config.buff3_enabled),
        ):
            if not enabled or not key or interval <= 0:
                continue
            with self._buff_timer_lock:
                due = now - self._last_buff[name] >= interval
                pending = self._buff_pending.get(name, False)
            if not due or pending:
                continue
            if name == "buff1":
                # 宠物食品 is a periodic drug, not an action buff.  It is
                # intentionally sent directly and never pauses patrol or
                # joins MotionArbiter's directional handoff.
                if self.key_sender.tap(key):
                    with self._buff_timer_lock:
                        self._last_buff[name] = time.monotonic()
                    LOG.warning("宠物食品 refresh: used %s (every %.0fs)",
                                key, interval)
                    self._arm_buff_verification(name, key)
                continue
            if self.motion_arbiter is not None:
                # The timer is deliberately NOT restarted when queued.  It
                # restarts only after the arbiter has tapped the key, waited
                # through the action window, and called this completion hook.
                def _completed(ok: bool, *, _name: str = name,
                               _key: str = key, _interval: float = interval) -> None:
                    with self._buff_timer_lock:
                        self._buff_pending[_name] = False
                        if ok:
                            self._last_buff[_name] = time.monotonic()
                    if ok:
                        LOG.warning("%s refresh: completed %s (every %.0fs)",
                                    _name.upper(), _key, _interval)
                        self._arm_buff_verification(_name, _key)
                    else:
                        LOG.warning("%s refresh: not sent; timer remains due",
                                    _name.upper())

                # Claim the local pending marker *before* handing the request
                # to the arbiter.  The arbiter can execute very quickly on an
                # idle patrol; claiming afterward could overwrite its already
                # delivered completion callback and leave the timer stuck.
                with self._buff_timer_lock:
                    self._buff_pending[name] = True
                if self.motion_arbiter.request_buff(key, _completed):
                    LOG.warning("%s refresh: queued %s (every %.0fs)",
                                name.upper(), key, interval)
                else:
                    with self._buff_timer_lock:
                        self._buff_pending[name] = False
                continue
            if self.key_sender.tap(key):
                with self._buff_timer_lock:
                    self._last_buff[name] = time.monotonic()
                LOG.warning("%s refresh: tapped %s (every %.0fs)",
                            name.upper(), key, interval)
                self._arm_buff_verification(name, key)

    def _process_frame(self, frame: object) -> None:
        self._check_buffs(time.monotonic())
        status_image = getattr(frame, "status_image", None)
        if hasattr(frame, "status_image"):
            if status_image is None:
                return
            image = status_image
        else:
            image = getattr(frame, "image", frame)
        if not isinstance(image, Image.Image):
            LOG.warning("ignored frame without PIL image")
            return
        reading = self.detector.detect(image)
        # Routine samples arrive at the shared capture cadence.  Keep them
        # available for diagnosis without burying movement and alert events.
        LOG.debug("status hp=%s mp=%s exp=%s confidence=%.2f",
                  reading.hp, reading.mp, reading.exp, reading.confidence)
        config = self.detector.config
        if reading.confidence < config.minimum_action_confidence:
            # Potions are the highest priority: a low-confidence read must NOT
            # block eating when a bar is below its threshold - a near-empty
            # bar is a tiny fill run, which reads with LOW confidence exactly
            # when the potion is needed.  Only suppress when nothing critical.
            hp_critical = (reading.hp_ratio is not None
                           and reading.hp_ratio < config.hp_ratio_threshold)
            mp_critical = (reading.mp_ratio is not None
                           and reading.mp_ratio < config.mp_ratio_threshold)
            if not hp_critical and not mp_critical:
                LOG.warning(
                    "status confidence %.2f below %.2f; potion actions suppressed",
                    reading.confidence, config.minimum_action_confidence,
                )
                self._low_count = {"hp": 0, "mp": 0}
                return
            LOG.warning(
                "status confidence %.2f low but %s below threshold; "
                "potions still attempted",
                reading.confidence,
                "HP" if hp_critical else "MP",
            )
        if self.status_state_path is not None:
            self._write_status_state(reading)
        now = time.monotonic()
        if config.hp_enabled:
            if not self._verify_potion_effect(
                "hp", reading.hp_ratio, config.hp_ratio_threshold,
                config.hp_key, now,
            ):
                self._check_resource(
                    "hp", reading.hp_ratio, config.hp_ratio_threshold,
                    config.hp_key, now,
                )
        else:
            self._low_count["hp"] = 0
            self._potion_verification["hp"] = None
        if config.mp_enabled:
            if not self._verify_potion_effect(
                "mp", reading.mp_ratio, config.mp_ratio_threshold,
                config.mp_key, now,
            ):
                self._check_resource(
                    "mp", reading.mp_ratio, config.mp_ratio_threshold,
                    config.mp_key, now,
                )
        else:
            self._low_count["mp"] = 0
            self._potion_verification["mp"] = None
        # Did each buff tap actually reach the game?  Reported from the HUD,
        # because the sender can only prove the key was emitted.
        self._verify_buff_effects(reading, now)
        self._last_hp = reading.hp
        self._last_mp = reading.mp

    def _write_status_state(self, reading: "StatusReading") -> None:
        """Publish the latest HP/MP ratios for other workers (JSON file)."""

        try:
            data = {
                "hp_ratio": reading.hp_ratio,
                "mp_ratio": reading.mp_ratio,
                "exp_ratio": reading.exp_ratio,
                "hp": reading.hp,
                "mp": reading.mp,
                "exp": reading.exp,
                "updated_at": time.time(),
            }
            Path(self.status_state_path).write_text(
                json.dumps(data), encoding="utf-8"
            )
        except OSError:
            LOG.warning("could not write status state", exc_info=True)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if (self.automation_active_event is not None
                        and not self.automation_active_event.is_set()):
                    continue
                self._process_frame(frame)
            except Exception:
                LOG.exception("status frame analysis failed")
            finally:
                try:
                    self.frame_queue.task_done()
                except (AttributeError, ValueError):
                    pass


def apply_drug_settings(config: StatusConfig, data: dict) -> StatusConfig:
    """Return a copy of ``config`` with the drug panel settings applied.

    ``data`` uses the UI's form: key names, integer percents (0..100) for
    ``hp_threshold``/``mp_threshold``, and MINUTES for the periodic buff
    ``buff1_interval``/``buff2_interval``/``buff3_interval`` (converted to
    seconds).  Unsupported or unknown keys are ignored (the existing binding
    stays).
    """

    kwargs: dict[str, object] = {}
    for field_name, data_key in (
        ("hp_key", "hp_key"), ("mp_key", "mp_key"),
        ("hp_enabled", "hp_enabled"), ("mp_enabled", "mp_enabled"),
        ("buff1_key", "buff1_key"), ("buff2_key", "buff2_key"),
        ("buff3_key", "buff3_key"),
        ("buff1_enabled", "buff1_enabled"),
        ("buff2_enabled", "buff2_enabled"),
        ("buff3_enabled", "buff3_enabled"),
    ):
        if data_key not in data:
            continue
        value = data[data_key]
        if field_name.endswith("key"):
            if value:
                key = str(value).casefold()
                if key in WindowKeySender._SCAN and key in BINDABLE_KEYS:
                    kwargs[field_name] = key
        elif isinstance(value, bool):
            kwargs[field_name] = value
    for field_name, data_key in (
        ("hp_ratio_threshold", "hp_threshold"),
        ("mp_ratio_threshold", "mp_threshold"),
    ):
        if data_key not in data:
            continue
        try:
            percent = float(data[data_key])
        except (TypeError, ValueError):
            continue
        kwargs[field_name] = float(np.clip(percent, 0.0, 100.0)) / 100.0
    # Periodic buff timers: UI sends minutes, the worker compares seconds.
    for field_name, data_key in (
        ("buff1_interval", "buff1_interval"),
        ("buff2_interval", "buff2_interval"),
        ("buff3_interval", "buff3_interval"),
    ):
        if data_key not in data:
            continue
        try:
            minutes = float(data[data_key])
        except (TypeError, ValueError):
            continue
        kwargs[field_name] = max(0.0, minutes * 60.0)
    return replace(config, **kwargs)


__all__: Sequence[str] = (
    "BarStatusDetector", "StatusConfig", "StatusReading", "StatusWorker",
    "WindowKeySender", "apply_drug_settings", "BINDABLE_KEYS",
)
