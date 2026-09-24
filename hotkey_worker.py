"""Physical-key-only global hotkeys for Maple Assistant UI actions."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_HOTKEY = 0x0312
WM_IME_CONTROL = 0x0283
IMC_GETOPENSTATUS = 0x0005
SMTO_ABORTIFHUNG = 0x0002
PM_REMOVE = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10

VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

# dwExtraInfo stamp placed on every key event Maple Assistant injects itself
# (status_worker.WindowKeySender._send_scan_code). The hook ignores only these
# self-marked events plus lower-integrity injection, so keystrokes injected by
# other same-integrity tools (Mouse Without Borders, ToDesk, remote-desktop
# clients) still trigger the chords.
SELF_INPUT_EXTRA_INFO = 0x4D4150  # "MAP"

# A recognized chord that the current mode suppresses (patrol running) is
# reported once per this window, so "nothing happened" is never silent while a
# held or repeated chord cannot flood the running log.
_SUPPRESSED_LOG_SECONDS = 5.0
# How often the active input method is re-checked while the worker runs.
_IME_CHECK_SECONDS = 2.0
# Re-sending the Ctrl key-up faster than the fastest Windows keyboard repeat
# rate (~31/s) is what keeps a chord's modifier from re-triggering the game's
# Ctrl attack while the operator still holds the key.
_CTRL_RELEASE_INTERVAL_SECONDS = 0.02
# After Ctrl+` toggles patrol, a forwarded Ctrl key-up can occasionally arrive
# late (or be lost) through Mouse Without Borders. Do not let its next plain
# arrow press look like a recording chord. A new physical Ctrl-down explicitly
# re-arms the configuration bindings.
_CONFIGURATION_ACTION_PREFIXES = (
    "record:",
    "record_jump_point",
    "select_next_layer",
    "select_next_patrol_start",
    "add_highest_layer",
    "delete_highest_layer",
)
# Mouse Without Borders can replay one Ctrl-down immediately after Ctrl+`.
# Past this short window, a Ctrl-down is an intentional new chord even if its
# matching forwarded Ctrl-up was lost.
_TOGGLE_CTRL_STALE_WINDOW_SECONDS = 0.35


def ime_status() -> str:
    """Describe the input method (IME) active for the foreground window.

    A CJK input method - Japanese, Chinese, Korean - intercepts Ctrl+letter and
    Ctrl+digit combinations for its own editing and mode switches, and while a
    composition is open it swallows most keys.  Those chords never reach
    Windows' hotkey table or this process, so a press produces no action AND no
    log line: the operator's "the Ctrl hotkeys do nothing at all" report, with
    nothing in the log to explain it.  Reporting the state makes that visible.
    """

    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
        user32.GetKeyboardLayout.restype = wintypes.HKL
        imm32.ImmIsIME.argtypes = (wintypes.HKL,)
        imm32.ImmIsIME.restype = wintypes.BOOL
        imm32.ImmGetContext.argtypes = (wintypes.HWND,)
        imm32.ImmGetContext.restype = ctypes.c_void_p
        imm32.ImmGetOpenStatus.argtypes = (ctypes.c_void_p,)
        imm32.ImmGetOpenStatus.restype = wintypes.BOOL
        imm32.ImmReleaseContext.argtypes = (wintypes.HWND, ctypes.c_void_p)
        imm32.ImmReleaseContext.restype = wintypes.BOOL
        imm32.ImmGetDefaultIMEWnd.argtypes = (wintypes.HWND,)
        imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
        user32.SendMessageTimeoutW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
        )
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "unknown (no foreground window)"
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(thread_id)
        layout = int(hkl or 0) & 0xFFFFFFFF
        if not imm32.ImmIsIME(hkl):
            return f"no IME (layout 0x{layout:04X})"
        open_status = None
        himc = imm32.ImmGetContext(hwnd)
        if himc:
            try:
                open_status = bool(imm32.ImmGetOpenStatus(himc))
            finally:
                imm32.ImmReleaseContext(hwnd, himc)
        if open_status is None:
            # The composition context is not readable for every window; the
            # IME's own window answers the open/close state instead.
            ime_hwnd = imm32.ImmGetDefaultIMEWnd(hwnd)
            if ime_hwnd:
                result = wintypes.DWORD(0)
                delivered = user32.SendMessageTimeoutW(
                    ime_hwnd, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0,
                    SMTO_ABORTIFHUNG, 200, ctypes.byref(result),
                )
                if delivered:
                    open_status = bool(result.value)
        if open_status is True:
            return (f"ON - it consumes Ctrl chords (layout 0x{layout:04X}); "
                    "switch it to 英数/半角英数 (or off) while playing, or set "
                    "hotkey.json delivery=hook")
        if open_status is False:
            return f"installed but off (layout 0x{layout:04X})"
        return (f"active, open/close state unreadable (layout 0x{layout:04X}); "
                "if the Ctrl chords do nothing, switch the IME to "
                "英数/半角英数 (or off), or set hotkey.json delivery=hook")
    except Exception:
        LOG.debug("IME state unavailable", exc_info=True)
        return "unknown"

KEY_VK = {
    "a": 0x41,
    "d": 0x44,
    "f": 0x46,
    "q": 0x51,
    "w": 0x57,
    "z": 0x5A,
    "0": 0x30,
    "1": 0x31,
    "2": 0x32,
    "3": 0x33,
    "4": 0x34,
    "5": 0x35,
    "6": 0x36,
    "7": 0x37,
    "8": 0x38,
    "9": 0x39,
    "left": 0x25,
    "up": 0x26,
    "down": 0x28,
    "right": 0x27,
    "home": 0x24,
    "insert": 0x2D,
    "delete": 0x2E,
    "bracketleft": 0xDB,
    "bracketright": 0xDD,
    "grave": 0xC0,
}


class HotkeyWorker(threading.Thread):
    """Observe operator Ctrl chords and queue actions for the Tk thread.

    The hook accepts a local keyboard or same-integrity remote input such as
    Mouse Without Borders, but ignores Maple Assistant's own tagged events
    and lower-integrity injection. Only a matched chord's second key is
    consumed; unrelated keyboard events continue through ``CallNextHookEx``.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        action_queue: "queue.Queue[str]",
        *,
        config_path: Optional[Path] = None,
        on_chord: Optional[Any] = None,
        keep_ctrl_released: Optional[Any] = None,
    ) -> None:
        super().__init__(name="hotkey-worker", daemon=True)
        self.stop_event = stop_event
        self.action_queue = action_queue
        self.config_path = Path(
            config_path or Path(__file__).with_name("hotkey.json")
        )
        # Runs after a configured chord is recognized, before its action is
        # queued.  It ends the GAME's Ctrl state (see ``release_ctrl`` below):
        # the game's own Ctrl key-down already arrived when the operator pressed
        # the chord, and while the operator keeps Ctrl held to finish it the
        # game's Ctrl-bound attack keeps firing - the "the attack is triggered
        # infinitely" report.
        self.on_chord = on_chord
        # A single injected key-up does NOT end a key the operator is still
        # physically holding: the keyboard repeat sends the Ctrl key-down again
        # and the game resumes attacking.  This callback re-asserts the release,
        # silently, for as long as Ctrl stays down after a chord.
        self.keep_ctrl_released = keep_ctrl_released
        self._ctrl_repeat_until = 0.0
        self._ctrl_repeat_next = 0.0
        self._ctrl_repeat_active = False
        self._ctrl_repeat_count = 0
        self.enabled = True
        self.ignore_injected = True
        self._bindings: dict[int, tuple[str, bool]] = {}
        self._ctrl_down = False
        self._configuration_requires_fresh_ctrl = False
        self._configuration_wait_for_ctrl_release = False
        self._configuration_toggle_at = float("-inf")
        self._fired: set[int] = set()
        self.cooldown_seconds = 2.0
        self._last_action_at: dict[str, float] = {}
        self._hook: Any = None
        self._hook_proc: Any = None
        # While patrol runs, every physical hotkey except the patrol-toggle
        # chord is temporarily disabled: automation owns the keyboard and a
        # stray Ctrl chord (for example a quick message) must not fire into
        # the game mid-route.  ``toggle_patrol`` stays live so patrol can
        # always be stopped from the keyboard.
        self._patrol_running = False
        # Rate limiting for the "recognized but suppressed" log line.
        self._suppressed_action = ""
        self._suppressed_reason = ""
        self._suppressed_log_at = float("-inf")
        self._load_config()

    def set_patrol_running(self, running: bool) -> None:
        """Disable all bindings except ``toggle_patrol`` while patrol runs."""

        self._patrol_running = bool(running)

    def _binding_allowed(self, action: str) -> bool:
        """True when this binding may fire in the current mode.

        While patrol runs most bindings are temporarily disabled so a stray
        chord cannot fire into the game mid-route.  The patrol-toggle chord
        stays live so patrol can always be stopped, and the fixed-attack
        interval adjustment stays live so the attack cadence can be tuned
        while patrol is running.
        """

        if not self._patrol_running:
            return True
        return (action == "toggle_patrol"
                or action.startswith("adjust_fixed_attack_interval:"))

    def _load_config(self) -> None:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOG.warning("hotkey config unavailable: %s", self.config_path)
            data = {}
        self.enabled = bool(data.get("enabled", True))
        self.ignore_injected = bool(data.get("ignore_injected", True))
        # "native" claims each chord with RegisterHotKey and uses this process's
        # low-level hook only for the chords Windows refused (Ctrl+` is the
        # common one).  "hook" recognizes every chord with the low-level hook
        # instead and registers nothing.
        #
        # Hook delivery is the default because it lets us distinguish this
        # assistant's own tagged SendInput events from human-controlled input.
        # Mouse Without Borders forwards a user's keyboard through Windows as
        # same-integrity injected input; that is deliberately accepted here.
        #
        # Known trade-off of native delivery with the Ctrl cleanup: the cleanup
        # injects a Ctrl key-up, which also clears the modifier state Windows
        # matches chords against, so a SECOND chord pressed without releasing
        # Ctrl in between can be missed.  Releasing Ctrl between chords (the
        # normal way to press Ctrl+` or Ctrl+1) always works, and "hook" mode
        # removes the effect entirely because it tracks the physical modifiers.
        release_ctrl = bool(data.get("release_ctrl_after_chord", True))
        self.release_ctrl_after_chord = release_ctrl
        delivery = str(data.get("delivery", "hook")).strip().casefold()
        self.delivery = delivery if delivery in ("native", "hook") else "hook"
        bindings: dict[int, tuple[str, bool]] = {}
        for item in data.get("bindings", []):
            if not isinstance(item, dict):
                continue
            chord = str(item.get("keys", "")).casefold().replace("`", "grave")
            parts = [part.strip() for part in chord.split("+")]
            if len(parts) != 2 or parts[0] != "ctrl":
                continue
            vk = KEY_VK.get(parts[1])
            action = str(item.get("action", "")).strip()
            if vk is not None and action:
                bindings[vk] = (action, bool(item.get("block_original", True)))
        self._bindings = bindings

    @staticmethod
    def _chord_name(vk: int) -> str:
        """Human-readable chord for one bound virtual key (for the log)."""

        for name, code in KEY_VK.items():
            if code == vk:
                return f"Ctrl+{name}"
        return f"Ctrl+VK_{vk:02X}"

    def _should_ignore_injected(self, flags: int, extra_info: int) -> bool:
        """Whether an injected key event must be dropped by the hook.

        Ignore only this assistant's tagged ``SendInput`` events and input
        injected from a lower-integrity process.  Same-integrity injected
        events are allowed because Mouse Without Borders uses precisely that
        mechanism to forward the operator's physical keyboard from another
        PC.  The assistant always stamps its own injected events with
        ``SELF_INPUT_EXTRA_INFO``, so accepting those external events cannot
        turn a Maple Assistant action into another hotkey action.
        """

        self_marked = int(extra_info) == SELF_INPUT_EXTRA_INFO
        # Mouse Without Borders and several remote-control tools deliver the
        # user's real keyboard through SendInput, sometimes with the lower
        # integrity flag. The assistant's own events are safely identified by
        # the private extra-info stamp, so only those must be ignored here.
        return self_marked

    # Actions that must never be rate limited.  These are the recording/selection steps the
    # operator drives by hand while mapping a floor (Ctrl+arrows, Ctrl+Down, Ctrl+Home,
    # Ctrl+Insert, Ctrl+Delete): the two-second cooldown silently swallowed fast presses and looked
    # like a dead hotkey (observed: four "hotkey cooldown: record:right_most_pos" in a row right
    # after the recordings stopped landing).  Both delivery paths already fire at most once per
    # physical press - MOD_NOREPEAT natively and the hook's per-key latch - so the cooldown adds
    # nothing for them.  Ctrl+Q (trade) and the fixed-attack interval keep their existing
    # exemption.
    _COOLDOWN_EXEMPT_PREFIXES = (
        "record:",
        "record_jump_point",
        "select_next_layer",
        "select_next_patrol_start",
        "add_highest_layer",
        "delete_highest_layer",
        "adjust_fixed_attack_interval:",
    )
    _COOLDOWN_EXEMPT_ACTIONS = ("trade:invite",)

    def _cooldown_exempt(self, action: str) -> bool:
        return (
            action in self._COOLDOWN_EXEMPT_ACTIONS
            or action.startswith(self._COOLDOWN_EXEMPT_PREFIXES)
        )

    def _note_suppressed(self, action: str, why: str) -> None:
        """Say out loud that a recognized chord is deliberately not delivered.

        While patrol runs every binding except the patrol toggle (and the
        attack-interval chords) is disabled on purpose.  That swallow used to be
        completely silent, so pressing Ctrl+1 during a route produced no action
        AND no log line - indistinguishable from a broken binding.
        """

        now = time.monotonic()
        if (action == self._suppressed_action
                and why == self._suppressed_reason
                and now - self._suppressed_log_at < _SUPPRESSED_LOG_SECONDS):
            return
        self._suppressed_action = action
        self._suppressed_reason = why
        self._suppressed_log_at = now
        LOG.info("hotkey %s ignored: %s", action, why)

    def _suppressed_reason_text(self, action: str) -> str:
        """Why a recognized chord cannot fire right now ('' when it can).

        Pure reporting: the delivery decisions themselves are unchanged from the
        release the operator confirmed working.
        """

        if not self.enabled:
            return "global hotkeys are switched off (hotkey.json enabled=false)"
        if not self._binding_allowed(action):
            return ("patrol is running (only Ctrl+` and the attack-interval "
                    "chords stay live)")
        return ""

    def _arm_ctrl_release(self, action: str) -> None:
        """Keep Ctrl released in the game while it is still physically held.

        The single release above is undone by the keyboard repeat of the held
        key within tens of milliseconds, which is why the game kept attacking
        after Ctrl+1 even though the cleanup ran.  While the operator's Ctrl is
        still down, re-assert the release until they let go.
        """

        if not callable(self.keep_ctrl_released) or not self._ctrl_down:
            return
        now = time.monotonic()
        # Stay armed until the real physical Ctrl key-up arrives.  A timeout
        # can re-enable a Ctrl-bound attack while the user is still holding a
        # slow Ctrl+Q chord, which is exactly the failure this protects.
        self._ctrl_repeat_until = float("inf")
        self._ctrl_repeat_next = now
        if not self._ctrl_repeat_active:
            self._ctrl_repeat_active = True
            self._ctrl_repeat_count = 0
            LOG.info(
                "hotkey %s: Ctrl is still held, keeping it released in the game "
                "until you let go (otherwise the keyboard repeat re-triggers the "
                "game's Ctrl attack)", action,
            )

    def _service_ctrl_release(self) -> None:
        """Re-assert the Ctrl release while a held Ctrl would re-trigger it."""

        if not self._ctrl_repeat_active:
            return
        now = time.monotonic()
        if not self._ctrl_down:
            self._ctrl_repeat_active = False
            LOG.info("hotkey Ctrl release finished after %d re-sends",
                     self._ctrl_repeat_count)
            return
        if now < self._ctrl_repeat_next:
            return
        # Faster than the fastest Windows keyboard repeat rate (~31/s), so the
        # game cannot see Ctrl held even for one frame.
        self._ctrl_repeat_next = now + _CTRL_RELEASE_INTERVAL_SECONDS
        try:
            self.keep_ctrl_released()
            self._ctrl_repeat_count += 1
        except Exception:
            LOG.warning("could not keep Ctrl released", exc_info=True)
            self._ctrl_repeat_active = False

    def _queue_action(self, action: str, *, native_delivery: bool = False) -> None:
        """Queue one recognized chord without corrupting native Ctrl state.

        Native registration owns the key lifecycle. It needs one immediate
        game-side Ctrl release, but must not enter the repeated-release loop:
        forwarded input can lose a Ctrl-up, leaving that loop armed forever and
        making later hotkeys appear dead. The loop remains for hook fallback
        chords, where the hook owns physical modifier tracking.
        """
        # Ctrl+[ / Ctrl+] are intentionally repeatable while the operator keeps
        # Ctrl held.  An injected Ctrl-up after the first press makes Windows
        # drop the modifier, so every later bracket press is lost.  Native
        # registration already consumes these chords; leave their modifier
        # lifecycle untouched so each press adjusts the interval by 0.1s.
        preserve_modifier = action.startswith("adjust_fixed_attack_interval:")
        if self.release_ctrl_after_chord and not preserve_modifier:
            callback = self.on_chord
            if callable(callback):
                try:
                    callback(action)
                except Exception:
                    LOG.warning("hotkey Ctrl cleanup failed for %s", action,
                                exc_info=True)
            if not native_delivery:
                self._arm_ctrl_release(action)
        # Exempt actions bypass the two-second cooldown; MOD_NOREPEAT / the hook key-up state still
        # prevent a held chord from firing repeatedly.
        repeatable = self._cooldown_exempt(action)
        now = time.monotonic()
        last = self._last_action_at.get(action)
        if (not repeatable and last is not None
                and now - last < self.cooldown_seconds):
            LOG.info("hotkey cooldown: %s", action)
            return
        try:
            self.action_queue.put_nowait(action)
            if not repeatable:
                self._last_action_at[action] = now
            LOG.info("hotkey triggered: %s", action)
        except queue.Full:
            LOG.warning("hotkey action queue full; ignored %s", action)

    def _install_hook(self, active_vks: set[int]) -> bool:
        """Install the low-level ``WH_KEYBOARD_LL`` hook.

        ``active_vks`` limits which chords this hook reacts to.  The hook is
        the fallback for chords Windows would not let the process claim
        natively (for example Ctrl+` when another application already owns
        that chord); chords native registration already delivered are excluded
        so one press can never fire twice.  When native registration is
        unavailable for every chord, ``active_vks`` is the whole binding set.
        """

        ULONG_PTR = wintypes.WPARAM

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        hook_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, hook_proc_type, ctypes.c_void_p, wintypes.DWORD
        )
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (
            ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        def hook_proc(code: int, message: int, l_param: int) -> int:
            if code != HC_ACTION:
                return user32.CallNextHookEx(self._hook, code, message, l_param)
            event = ctypes.cast(
                l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
            ).contents
            if self._should_ignore_injected(
                int(event.flags), int(event.dwExtraInfo)
            ):
                return user32.CallNextHookEx(self._hook, code, message, l_param)
            # The assistant's own chord cleanup injects a Ctrl key-up (to end a
            # Ctrl-bound attack in the game).  That event is never a physical
            # change of the operator's modifier state, so it must not clear
            # ``_ctrl_down`` even when ignore_injected is switched off -
            # otherwise every later chord of the same Ctrl-hold would be lost.
            if int(event.dwExtraInfo) == SELF_INPUT_EXTRA_INFO:
                return user32.CallNextHookEx(self._hook, code, message, l_param)

            vk = int(event.vkCode)
            is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = message in (WM_KEYUP, WM_SYSKEYUP)
            if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                if is_down:
                    self._ctrl_down = True
                    # A real Ctrl-down after Ctrl+` is the only event that
                    # clears the stale-modifier safety gate. The assistant's
                    # injected Ctrl-up is filtered above and can never affect
                    # this physical state.
                    stale_window_elapsed = (
                        time.monotonic() - self._configuration_toggle_at
                        >= _TOGGLE_CTRL_STALE_WINDOW_SECONDS
                    )
                    if (not self._configuration_wait_for_ctrl_release
                            or stale_window_elapsed):
                        self._configuration_wait_for_ctrl_release = False
                        self._configuration_requires_fresh_ctrl = False
                elif is_up:
                    self._ctrl_down = False
                    if self._configuration_wait_for_ctrl_release:
                        self._configuration_wait_for_ctrl_release = False
                    # The hardware key-up normally reaches the game too; one
                    # final forced release closes the small race with a
                    # queued/repeated Ctrl attack event before standing down.
                    if self._ctrl_repeat_active:
                        try:
                            self.keep_ctrl_released()
                        except Exception:
                            LOG.debug("final physical Ctrl release cleanup failed", exc_info=True)
                        self._ctrl_repeat_active = False
                return user32.CallNextHookEx(self._hook, code, message, l_param)

            if vk not in active_vks:
                # Claimed natively (or unbound): leave it to the native
                # message queue / other applications untouched.
                return user32.CallNextHookEx(self._hook, code, message, l_param)

            binding = self._bindings.get(vk)
            if binding is not None:
                action, block_original = binding
                # A remote Ctrl-down can arrive just after the paired key
                # event. If Windows could not reserve this native chord, the
                # fallback hook must still recognize and consume it instead
                # of leaking Ctrl+[ / Ctrl+] to the game's own hotkeys.
                ctrl_held = self._ctrl_down or bool(
                    int(user32.GetAsyncKeyState(VK_CONTROL)) & 0x8000
                )
                if ctrl_held:
                    self._ctrl_down = True
                if (is_down and self._configuration_requires_fresh_ctrl
                        and action.startswith(_CONFIGURATION_ACTION_PREFIXES)):
                    # Let the plain physical arrow/home/insert/delete reach
                    # the game. It is not a fresh Ctrl chord merely because a
                    # remote-input Ctrl key-up was delayed after Ctrl+`.
                    self._note_suppressed(
                        action, "waiting for a new physical Ctrl press after Ctrl+`"
                    )
                    # Mouse Without Borders can lose the physical Ctrl-up and
                    # keep forwarding a stale modifier indefinitely. This
                    # first plain configuration key proves the latch is stale:
                    # clear it locally so later arrows are ordinary game input
                    # and the next deliberate Ctrl press can create a chord.
                    self._ctrl_down = False
                    self._ctrl_repeat_active = False
                    self._configuration_requires_fresh_ctrl = False
                    self._configuration_wait_for_ctrl_release = False
                    return user32.CallNextHookEx(
                        self._hook, code, message, l_param
                    )
                if not self._binding_allowed(action):
                    # Patrol is running: this chord is temporarily disabled.
                    # Treat it as unbound - never queue it and do not consume
                    # it, so the key still reaches the game/other apps.  Say so:
                    # a silently swallowed chord is indistinguishable from a
                    # broken binding.
                    self._note_suppressed(
                        action, "patrol is running (only Ctrl+` and the "
                                "attack-interval chords stay live)",
                    )
                    return user32.CallNextHookEx(
                        self._hook, code, message, l_param
                    )
                was_fired = vk in self._fired
                repeatable = action.startswith("adjust_fixed_attack_interval:")
                if is_up:
                    # A held physical key may only fire once.  In particular,
                    # releasing and pressing Ctrl again while still holding
                    # the other key must not turn it into a second action.
                    self._fired.discard(vk)
                elif is_down and (repeatable or not was_fired):
                    if self.enabled and ctrl_held:
                        if not repeatable:
                            self._fired.add(vk)
                        self._queue_action(action)
                    elif not self.enabled:
                        self._note_suppressed(
                            action, "global hotkeys are switched off "
                                    "(hotkey.json enabled=false)",
                        )
                if block_original and (ctrl_held or was_fired):
                    return 1
            return user32.CallNextHookEx(self._hook, code, message, l_param)

        self._hook_proc = hook_proc_type(hook_proc)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0
        )
        return bool(self._hook)

    def _uninstall_hook(self) -> None:
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._hook_proc = None

    def run(self) -> None:
        if sys.platform != "win32":
            LOG.warning("global hotkeys require Windows")
            self.stop_event.wait()
            return

        self._run_hotkeys()

    def _run_hotkeys(self) -> None:
        """Deliver every binding: native registration where Windows allows it,
        and the low-level hook for every chord it would not claim.

        ``RegisterHotKey`` is preferred for this fixed set of Ctrl chords (it
        consumes the chord before the game sees it and uses ``MOD_NOREPEAT``),
        but a chord another application already owns - Ctrl+` is the common
        case - fails to register.  The process used to fall back to the hook
        only when *no* chord registered, so a partially rejected set left
        those chords silently dead.  The hook now covers exactly the chords
        native registration could not claim, so Ctrl+` always works.
        """

        if not self._bindings:
            LOG.info("hotkey worker: no bindings configured")
            self.stop_event.wait()
            return

        # The active input method decides whether the physical chords reach this
        # process at all, so report it up front instead of leaving the operator
        # with hotkeys that do nothing and a log that says nothing.
        ime_state = ime_status()
        LOG.info("hotkey IME state: %s", ime_state)
        LOG.info(
            "hotkey settings: delivery=%s (%s), release_ctrl_after_chord=%s, "
            "enabled=%s, ignore_injected=%s",
            self.delivery,
            "low-level hook for every chord - opt-in for an active IME"
            if self.delivery == "hook"
            else "RegisterHotKey, hook only for chords Windows refuses "
                 "(set delivery=hook if an IME swallows the chords)",
            self.release_ctrl_after_chord,
            self.enabled,
            self.ignore_injected,
        )

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = (
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        )
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL

        registered: dict[int, int] = {}  # hotkey id -> virtual key
        hook_vks: set[int] = set()
        if self.delivery == "hook":
            # Every chord through this process's own low-level hook, nothing
            # claimed natively (native registration would deliver each press a
            # second time).  This is the mode for an active CJK input method.
            hook_vks = set(self._bindings)
            LOG.info(
                "hotkey delivery: low-level hook for all %d chords "
                "(hotkey.json delivery=hook); nothing registered natively",
                len(hook_vks),
            )
        else:
            for hotkey_id, (vk, _binding) in enumerate(
                sorted(self._bindings.items()), start=1
            ):
                if user32.RegisterHotKey(
                    None, hotkey_id, MOD_CONTROL | MOD_NOREPEAT, vk
                ):
                    registered[hotkey_id] = vk
                else:
                    LOG.warning(
                        "could not register hotkey Ctrl+VK_%02X natively; "
                        "using the low-level hook for it", vk,
                    )

        native_vks = set(registered.values())
        hook_vks = hook_vks or (set(self._bindings) - native_vks)
        LOG.info(
            "hotkey worker started native=%d/%d hook-fallback=%d",
            len(registered), len(self._bindings), len(hook_vks),
        )
        if hook_vks and self.delivery != "hook":
            # Name the chords instead of reporting only a count: this line is
            # the operator's only evidence when "the hotkeys do not work", and
            # a chord another application (or a still-running assistant) already
            # owns - an active CJK input method is the common case - is consumed
            # before this process can see it, so the hook fallback cannot rescue
            # it either.
            LOG.warning(
                "hotkey chords NOT claimed natively: %s - another program "
                "(an active IME, or a still-running assistant) owns them, so "
                "presses may never reach this window; the low-level hook covers "
                "only the chords no other process registered",
                ", ".join(sorted(self._chord_name(vk) for vk in hook_vks)),
            )

        # Native hotkeys deliberately stay hook-free. RegisterHotKey owns a
        # complete key lifecycle; adding a modifier-tracking hook merely to
        # repeat injected Ctrl-up events was the repeated-use failure path for
        # forwarded Mouse Without Borders input. Install a hook only for chords
        # Windows actually refused to register.
        hook_ok = True
        if hook_vks:
            hook_ok = self._install_hook(hook_vks)
            if not hook_ok:
                LOG.warning("could not install global hotkey hook")
        if not registered and not hook_ok:
            # Hotkeys are optional. A hook permission failure must not make
            # the core-worker supervisor shut down normal gameplay.
            self.stop_event.wait()
            return

        message = wintypes.MSG()
        next_ime_check = time.monotonic() + _IME_CHECK_SECONDS
        try:
            while not self.stop_event.is_set():
                while user32.PeekMessageW(
                    ctypes.byref(message), None, 0, 0, PM_REMOVE
                ):
                    if message.message == WM_HOTKEY:
                        vk = registered.get(int(message.wParam))
                        action = (
                            self._bindings[vk][0] if vk is not None else None
                        )
                        if action is not None:
                            if self.enabled and self._binding_allowed(action):
                                self._queue_action(action, native_delivery=True)
                            else:
                                # Recognized by Windows but dropped here: say
                                # why, instead of leaving the operator with a
                                # chord that does nothing and logs nothing.
                                self._note_suppressed(
                                    action, self._suppressed_reason_text(action)
                                    or "the chord is not delivered",
                                )
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                now = time.monotonic()
                self._service_ctrl_release()
                if now >= next_ime_check:
                    # A changed input method changes whether the chords arrive
                    # at all, so its transitions are logged with the reason.
                    next_ime_check = now + _IME_CHECK_SECONDS
                    state = ime_status()
                    if state != ime_state:
                        ime_state = state
                        LOG.info("hotkey IME state changed: %s", state)
                self.stop_event.wait(0.01)
        finally:
            self._uninstall_hook()
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            LOG.info("hotkey worker stopped")


__all__ = ["HotkeyWorker"]
