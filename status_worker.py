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
from typing import Optional, Protocol, Sequence

import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)

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
        self._key_owners: dict[str, int] = {}
        self._input_enabled = threading.Event()
        if input_enabled:
            self._input_enabled.set()
        self.hwnd: Optional[int] = None

    def enable_input(self) -> None:
        """Allow workers to emit keyboard events after explicit UI activation."""

        self._input_enabled.set()
        LOG.info("live keyboard input enabled")

    def disable_input(self) -> None:
        """Block new keyboard events and release every currently held key."""

        self._input_enabled.clear()
        self.release_all_keys()
        LOG.info("live keyboard input disabled")

    def release_all_keys(self) -> None:
        """Release owned keys without changing the UI-controlled input state."""

        with self._key_state_lock:
            held_keys = tuple(self._key_owners)
            self._key_owners.clear()
        if not self.dry_run:
            for key in held_keys:
                scan_code, extended = self._SCAN[key]
                self._send_scan_code(scan_code, key_up=True, extended=extended)
        if held_keys:
            LOG.info("released held keys: %s", ", ".join(held_keys))

    def input_is_enabled(self) -> bool:
        return self._input_enabled.is_set()

    def _find_target_window(self) -> int:
        """Find the configured game window without blocking the UI forever."""

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
        matches: list[int] = []
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
            if self.window_title.casefold() in title.casefold():
                matches.append(hwnd)

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
        LOG.info("WINDOW SELECT: fallback title scan found %d matching window(s)",
                 len(matches))
        if len(matches) != 1:
            raise OSError(
                f"expected exactly one visible game window containing "
                f"{self.window_title!r}; found {len(matches)}"
            )
        self.hwnd = matches[0]
        return matches[0]

    def select_window(self) -> bool:
        """Restore and foreground the configured game window automatically."""

        if self.dry_run:
            return True
        import win32api
        import win32con
        import win32gui
        import win32process

        LOG.info("WINDOW SELECT: waiting for selection lock")
        with self._selection_lock:
            LOG.info("WINDOW SELECT: selection lock acquired")
            # A game can recreate its top-level window while keeping the same
            # title. Never rely on an HWND cached by a previous selection.
            hwnd = self._find_target_window()
            LOG.info("WINDOW SELECT: found hwnd=%s", hwnd)
            try:
                # Bring the game to the foreground WITHOUT pressing Alt (Alt is
                # the game's JUMP key).  Windows can refuse briefly (foreground
                # lock, or the assistant runs at a different privilege than the
                # game), so retry direct SetForegroundWindow + thread-input
                # attachment a few times before giving up.
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
                            LOG.info("WINDOW SELECT: activation verified")
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
                            LOG.info("WINDOW SELECT: activation verified")
                            return True
                    time.sleep(0.1)

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

    def press(self, key: str, duration: float = 0.025) -> bool:
        """Press a key using native SendInput scan-code keyboard events only."""

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        # Movement holds may intentionally last multiple seconds.  The former
        # 0.5-second upper clamp silently shortened a requested 2-second hold.
        duration = float(np.clip(duration, 0.01, 10.0))
        started = time.monotonic()
        claimed = False
        try:
            claimed = self.key_down(key)
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
                self.key_up(key)
                LOG.info("key hold complete=%s actual_hold=%.3fs", key,
                         time.monotonic() - started)

    def key_down(self, key: str) -> bool:
        """Claim a key; inject key-down only for the first concurrent owner."""

        key = key.casefold()
        if key not in self._SCAN:
            raise ValueError(f"unsupported key: {key}")
        if not self.input_is_enabled():
            LOG.debug("blocked key-down=%s: live input is not enabled", key)
            return False
        if self.dry_run:
            LOG.info("DRY-RUN key-down=%s target=%r", key, self.window_title)
        elif not self._foreground_matches():
            LOG.debug("blocked key-down=%s: game window is not foreground", key)
            return False
        with self._key_state_lock:
            owners = self._key_owners.get(key, 0)
            if owners == 0 and not self.dry_run:
                scan_code, extended = self._SCAN[key]
                self._send_scan_code(scan_code, key_up=False, extended=extended)
            self._key_owners[key] = owners + 1
        LOG.info("key-down=%s owners=%d", key, owners + 1)
        return True

    def key_up(self, key: str) -> bool:
        """Release one claim; inject key-up only after the final owner exits."""

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
                if not self.dry_run:
                    scan_code, extended = self._SCAN[key]
                    self._send_scan_code(scan_code, key_up=True, extended=extended)
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
        with self._key_state_lock:
            owned = self._key_owners.get(key, 0) > 0
        if not owned:
            return False
        if self.dry_run:
            LOG.info("DRY-RUN repeat key-down=%s", key)
            return True
        if not self._foreground_matches():
            return False
        scan_code, extended = self._SCAN[key]
        self._send_scan_code(scan_code, key_up=False, extended=extended)
        return True

    def is_key_down(self, key: str) -> bool:
        """Return whether any worker currently owns the key."""

        with self._key_state_lock:
            return self._key_owners.get(key.casefold(), 0) > 0

    def tap(self, key: str) -> bool:
        return self.press(key, duration=0.025)

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
            direct_tap("enter")
            # Older/slower clients need time to open chat before Ctrl+V and
            # to consume the clipboard paste before the final Enter.
            time.sleep(0.15)
            transition("ctrl", False)
            direct_tap("v")
            transition("ctrl", True)
            time.sleep(0.15)
            direct_tap("enter")
        LOG.info("quick message pasted and sent to game window")
        return True

    @staticmethod
    def _send_scan_code(scan_code: int, *, key_up: bool, extended: bool) -> None:
        """Inject one hardware-like keyboard transition with Win32 SendInput."""

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
        event = INPUT(type=1, ki=KEYBDINPUT(0, scan_code, flags, 0, 0))
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
    The capture region is the FIXED-PIXEL 370x57 bottom-middle info bar;
    inside it three bars sit SIDE BY SIDE in the same vertical band - HP
    (red) left, MP (blue) middle, EXP (yellow) right.  Each bar is measured
    ONLY inside its own horizontal zone (``bar_zones``, fractions of the ROI
    width) so the three can never be mixed up.
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
    # Three side-by-side bars in the fixed-pixel 370x57 info bar (measured
    # on the real client: HP red x ~7-91, MP blue x ~96-230, EXP yellow
    # x ~237-363, all in the same vertical band).  Zones are (name, left,
    # right) fractions of the ROI width so the bars can never be mixed.
    bar_zones: tuple[tuple[str, float, float], ...] = (
        ("hp", 0.02, 0.25),
        ("mp", 0.26, 0.63),
        ("exp", 0.64, 1.00),
    )
    # Vertical band (top, bottom) as fractions of the ROI height: the bars
    # occupy rows ~33-53 of the 57px capture; the band excludes the blue
    # UI text/decoration above them (rows 7-9).
    bar_band: tuple[float, float] = (0.50, 0.96)
    # Full bar length per bar as a fraction of the ROI width (FIXED PIXEL
    # HUD - measured on the real client: HP ~85px, MP ~135px, EXP ~127px
    # inside the 370px-wide capture).  Accepted candidates may vary.
    full_bar_width_fractions: dict[str, float] = field(
        default_factory=lambda: {
            "hp": 85.0 / 370.0,
            "mp": 135.0 / 370.0,
            "exp": 127.0 / 370.0,
        }
    )
    min_bar_width_fraction: float = 5.0 / 370.0
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
    ) -> None:
        super().__init__(name="status-worker", daemon=True)
        self.frame_queue = frame_queue
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.detector = detector or BarStatusDetector()
        self.automation_active_event = automation_active_event
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
        # Monotonic timestamps of the last periodic buff tap (per buff row).
        # 增益为"定时触发"，不从开局立即触发：起始时间戳设为当前时刻，
        # 第一个增益会在 interval 秒后才按（用户会先手动触发第一次增益）。
        self._last_buff = {"buff1": time.monotonic(), "buff2": time.monotonic(),
                          "buff3": time.monotonic()}

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
            if now - self._last_buff[name] < interval:
                continue
            if self.key_sender.tap(key):
                self._last_buff[name] = now
                LOG.warning("%s refresh: tapped %s (every %.0fs)",
                            name.upper(), key, interval)

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
        LOG.info("status hp=%s mp=%s exp=%s confidence=%.2f",
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
