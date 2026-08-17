"""Character status detection and safe keyboard actions.

This module deliberately does not capture the screen.  ``StatusWorker`` consumes
the immutable frames published by ``capture_worker`` so all decisions are made
from one coherent screenshot.
"""

from __future__ import annotations

import logging
import ctypes
import queue
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional, Protocol, Sequence

import numpy as np
from PIL import Image

LOG = logging.getLogger(__name__)

# Keys the UI bind buttons may capture - game-usable hotkeys only: the
# modifiers (except alt, which is the jump key), navigation/edit keys, and
# the 1-9 number row above the letters.  Everything else (letters, F-keys,
# numpad, punctuation) is intentionally not bindable to avoid conflicts
# with game bindings.
BINDABLE_KEYS = frozenset({
    "shift", "ctrl", "space", "delete", "end",
    "pagedown", "pageup", "home", "insert",
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
    ) -> None:
        self.window_title = window_title
        self.dry_run = dry_run
        self.targets_configured_window = True
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
        """Find exactly one visible top-level window containing the title."""

        import win32gui

        matches: list[int] = []

        def collect(hwnd: int, _extra: object) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if self.window_title.casefold() in title.casefold():
                matches.append(hwnd)

        win32gui.EnumWindows(collect, None)
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
                if win32gui.IsIconic(hwnd):
                    # ShowWindow sends a synchronous message to the game and
                    # can freeze Tk indefinitely while the game thread is busy.
                    # An already-visible window needs no ShowWindow call at all.
                    win32gui.ShowWindowAsync(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.05)
                    LOG.info("WINDOW SELECT: minimized game restored asynchronously")

                # This succeeds directly on many systems and avoids needless
                # AttachThreadInput calls (which can return ERROR_INVALID_PARAMETER).
                if win32gui.GetForegroundWindow() != hwnd:
                    try:
                        # BringWindowToTop synchronously sends a window-manager
                        # message to the target. Some games stop pumping that
                        # message while rendering and can consequently freeze
                        # the Tk callback forever. SetForegroundWindow is enough
                        # here because the click gave this process foreground
                        # activation rights.
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        LOG.debug("direct foreground selection was refused", exc_info=True)

                # Start Patrol is clicked from our foreground Tk window, so the
                # direct activation normally succeeds. Return immediately and
                # avoid unnecessary Alt/AttachThreadInput fallbacks.
                if win32gui.GetForegroundWindow() == hwnd:
                    LOG.info("WINDOW SELECT: direct activation verified")
                    return True

                # A brief Alt transition lets Windows accept a foreground request
                # after its foreground-lock timeout in most interactive sessions.
                if win32gui.GetForegroundWindow() != hwnd:
                    self._send_scan_code(0x38, key_up=False, extended=False)
                    self._send_scan_code(0x38, key_up=True, extended=False)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        LOG.debug("Alt-assisted foreground selection was refused",
                                  exc_info=True)
                    time.sleep(0.05)

                # Thread-input attachment is only a final best-effort fallback.
                # Failure to attach one thread must not abort the other activation
                # methods or surface raw Win32 error 87 to the user.
                foreground = win32gui.GetForegroundWindow()
                if foreground != hwnd and foreground:
                    current_tid = win32api.GetCurrentThreadId()
                    foreground_tid = win32process.GetWindowThreadProcessId(foreground)[0]
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
                                    LOG.debug("could not attach input thread %s",
                                              thread_id, exc_info=True)
                        try:
                            win32gui.SetForegroundWindow(hwnd)
                        except Exception:
                            LOG.debug("attached foreground selection was refused",
                                      exc_info=True)
                        time.sleep(0.05)
                    finally:
                        for thread_id in reversed(attached_threads):
                            try:
                                win32process.AttachThreadInput(current_tid, thread_id, False)
                            except Exception:
                                LOG.debug("could not detach input thread %s", thread_id,
                                          exc_info=True)

                if win32gui.GetForegroundWindow() != hwnd:
                    raise OSError(
                        "Windows refused to foreground the dynamically selected game window"
                    )
                LOG.info("WINDOW SELECT: fallback activation verified")
                return True
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
            LOG.warning("cannot verify foreground window: %s", exc)
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


@dataclass(frozen=True)
class StatusConfig:
    """Calibration values for the classic bottom-centre HP/MP bars.

    ``status_roi`` is (left, top, right, bottom) in normalized frame units.
    The default maximums match the currently observed character and should be
    changed after equipment/stat changes.
    """

    status_roi: tuple[float, float, float, float] = (0.34, 0.96, 0.56, 1.0)
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
    # Approximate full bar length; accepted candidates may vary substantially.
    # The observed 2560x1600 client scales to a 1707px-wide capture where each
    # fill is about 131px (131/1707 ~= 0.077).
    full_bar_width_fraction: float = 0.077
    min_bar_width_fraction: float = 0.003
    minimum_action_confidence: float = 0.55


class BarStatusDetector:
    """Find red HP and blue MP horizontal fills without OCR.

    The broad lower-middle ROI makes this resolution independent.  Confidence is
    deliberately conservative: ambiguous/missing bars produce ``None`` and no
    potion action instead of guessing.
    """

    def __init__(self, config: StatusConfig = StatusConfig()) -> None:
        self.config = config

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

    def _ratio(self, mask: np.ndarray, frame_width: int) -> tuple[Optional[float], float]:
        run, row, start = self._longest_run(mask)
        minimum = frame_width * self.config.min_bar_width_fraction
        if run < minimum:
            return None, 0.0

        # Merge several neighbouring scanlines. Anti-aliasing/borders otherwise
        # make a one-row estimate unnecessarily fragile.
        top, bottom = max(0, row - 2), min(mask.shape[0], row + 3)
        local_runs = [self._longest_run(mask[y:y + 1])[0] for y in range(top, bottom)]
        run = int(np.median([value for value in local_runs if value]))
        expected = max(1.0, frame_width * self.config.full_bar_width_fraction)
        ratio = float(np.clip(run / expected, 0.0, 1.0))
        confidence = min(1.0, run / minimum) * (0.75 if ratio >= 0.995 else 1.0)
        return ratio, confidence

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

        red, green, blue = crop[..., 0], crop[..., 1], crop[..., 2]
        # Saturated red/blue fills, allowing bright highlights and dark shading.
        hp_mask = (red >= 90) & (red >= green * 1.35) & (red >= blue * 1.25)
        mp_mask = (blue >= 90) & (blue >= red * 1.25) & (blue >= green * 1.10)
        hp_ratio, hp_conf = self._ratio(hp_mask, width)
        mp_ratio, mp_conf = self._ratio(mp_mask, width)
        hp = round(hp_ratio * self.config.max_hp) if hp_ratio is not None else None
        mp = round(mp_ratio * self.config.max_mp) if mp_ratio is not None else None
        confidence = min(hp_conf, mp_conf) if hp is not None and mp is not None else 0.0
        return StatusReading(hp, mp, hp_ratio, mp_ratio, confidence)


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
        low_frames_required: int = 2,
    ) -> None:
        super().__init__(name="status-worker", daemon=True)
        self.frame_queue = frame_queue
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.detector = detector or BarStatusDetector()
        self.automation_active_event = automation_active_event
        self.potion_cooldown = max(0.0, potion_cooldown)
        self.low_frames_required = max(1, low_frames_required)
        self._low_count = {"hp": 0, "mp": 0}
        self._last_potion = {"hp": float("-inf"), "mp": float("-inf")}

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
            if self.key_sender.tap(key):
                self._last_potion[name] = now
                self._low_count[name] = 0
                LOG.warning("%s=%.0f%% below %.0f%%: used %s", name.upper(),
                            ratio * 100, threshold_ratio * 100, key)

    def _process_frame(self, frame: object) -> None:
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
        LOG.info("status hp=%s mp=%s confidence=%.2f", reading.hp, reading.mp,
                 reading.confidence)
        if reading.confidence < self.detector.config.minimum_action_confidence:
            LOG.warning("status confidence %.2f below %.2f; potion actions suppressed",
                        reading.confidence,
                        self.detector.config.minimum_action_confidence)
            self._low_count = {"hp": 0, "mp": 0}
            return
        now = time.monotonic()
        config = self.detector.config
        if config.hp_enabled:
            self._check_resource(
                "hp", reading.hp_ratio, config.hp_ratio_threshold,
                config.hp_key, now,
            )
        else:
            self._low_count["hp"] = 0
        if config.mp_enabled:
            self._check_resource(
                "mp", reading.mp_ratio, config.mp_ratio_threshold,
                config.mp_key, now,
            )
        else:
            self._low_count["mp"] = 0

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

    ``data`` uses the UI's form: key names and integer percents (0..100) for
    ``hp_threshold``/``mp_threshold``.  Unsupported or unknown keys are
    ignored (the existing binding stays).
    """

    kwargs: dict[str, object] = {}
    for field_name, data_key in (
        ("hp_key", "hp_key"), ("mp_key", "mp_key"),
        ("hp_enabled", "hp_enabled"), ("mp_enabled", "mp_enabled"),
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
    return replace(config, **kwargs)


__all__: Sequence[str] = (
    "BarStatusDetector", "StatusConfig", "StatusReading", "StatusWorker",
    "WindowKeySender", "apply_drug_settings", "BINDABLE_KEYS",
)
