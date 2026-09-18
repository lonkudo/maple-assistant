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

KEY_VK = {
    "a": 0x41,
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
    """Observe physical Ctrl chords and queue actions for the Tk thread.

    Events injected by Maple Assistant itself - every SendInput key event is
    stamped with ``SELF_INPUT_EXTRA_INFO`` in ``WindowKeySender`` - and
    lower-integrity injected events are ignored, so the assistant's own
    gameplay keys (the attack Ctrl presses, Ctrl+V quick-message chords) can
    never drive the chord state machine. Same-integrity injection from other
    tools (Mouse Without Borders, ToDesk, remote desktops) is treated like
    physical input. Only a matched chord's second key is consumed; all
    unrelated keyboard events continue through ``CallNextHookEx`` unchanged.
    """

    def __init__(
        self,
        stop_event: threading.Event,
        action_queue: "queue.Queue[str]",
        *,
        config_path: Optional[Path] = None,
    ) -> None:
        super().__init__(name="hotkey-worker", daemon=True)
        self.stop_event = stop_event
        self.action_queue = action_queue
        self.config_path = Path(
            config_path or Path(__file__).with_name("hotkey.json")
        )
        self.enabled = True
        self.ignore_injected = True
        self._bindings: dict[int, tuple[str, bool]] = {}
        self._ctrl_down = False
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

    def _should_ignore_injected(self, flags: int, extra_info: int) -> bool:
        """Whether an injected key event must be dropped by the hook.

        With ``ignore_injected`` enabled the hook ignores only events injected
        by Maple Assistant itself (they carry ``SELF_INPUT_EXTRA_INFO`` and
        include attack Ctrl presses and Ctrl+V quick-message chords) and
        lower-integrity injection (``LLKHF_LOWER_IL_INJECTED``, a spoofing
        vector). Foreign same-integrity injection such as Mouse Without
        Borders keystrokes is treated like physical input. An explicit
        ``ignore_injected=false`` accepts every event, exactly as before this
        distinction existed.
        """

        if not self.ignore_injected:
            return False
        lower_il = bool(flags & LLKHF_LOWER_IL_INJECTED)
        self_marked = int(extra_info) == SELF_INPUT_EXTRA_INFO
        return lower_il or self_marked

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

    def _queue_action(self, action: str) -> None:
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

            vk = int(event.vkCode)
            is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = message in (WM_KEYUP, WM_SYSKEYUP)
            if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                if is_down:
                    self._ctrl_down = True
                elif is_up:
                    self._ctrl_down = False
                return user32.CallNextHookEx(self._hook, code, message, l_param)

            if vk not in active_vks:
                # Claimed natively (or unbound): leave it to the native
                # message queue / other applications untouched.
                return user32.CallNextHookEx(self._hook, code, message, l_param)

            binding = self._bindings.get(vk)
            if binding is not None:
                action, block_original = binding
                if not self._binding_allowed(action):
                    # Patrol is running: this chord is temporarily disabled.
                    # Treat it as unbound - never queue it and do not consume
                    # it, so the key still reaches the game/other apps.
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
                elif (self.enabled and self._ctrl_down
                      and is_down and (repeatable or not was_fired)):
                    if not repeatable:
                        self._fired.add(vk)
                    self._queue_action(action)
                if block_original and (self._ctrl_down or was_fired):
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

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = (
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        )
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL

        registered: dict[int, int] = {}  # hotkey id -> virtual key
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
        hook_vks = set(self._bindings) - native_vks
        LOG.info(
            "hotkey worker started native=%d/%d hook-fallback=%d",
            len(registered), len(self._bindings), len(hook_vks),
        )

        # The hook is installed only when a chord actually needs it.  A fully
        # native set stays hook-free (its chords are consumed by Windows and
        # never reach the hook); an all-native failure leaves hook_vks holding
        # every binding, exactly like the old full fallback.
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
                        if (action is not None and self.enabled
                                and self._binding_allowed(action)):
                            self._queue_action(action)
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                self.stop_event.wait(0.01)
        finally:
            self._uninstall_hook()
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            LOG.info("hotkey worker stopped")


__all__ = ["HotkeyWorker"]
