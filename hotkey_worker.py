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
PM_REMOVE = 0x0001
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10

VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

KEY_VK = {
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

    Assistant-generated ``SendInput`` events carry ``LLKHF_INJECTED`` and are
    ignored. No custom ``dwExtraInfo`` marker is used. Only a matched chord's
    second key is consumed; all unrelated physical keyboard events continue
    through ``CallNextHookEx`` unchanged.
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
        """True when this binding may fire in the current mode."""

        if not self._patrol_running:
            return True
        return action == "toggle_patrol"

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

    def _queue_action(self, action: str) -> None:
        repeatable = action.startswith("adjust_fixed_attack_interval:")
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

    def run(self) -> None:
        if sys.platform != "win32":
            LOG.warning("global hotkeys require Windows")
            self.stop_event.wait()
            return

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
            injected = bool(
                event.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)
            )
            if self.ignore_injected and injected:
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
        if not self._hook:
            LOG.warning("could not install global hotkey hook")
            # Hotkeys are optional. A hook permission failure must not make
            # the core-worker supervisor shut down normal gameplay.
            self.stop_event.wait()
            return
        LOG.info("hotkey worker started bindings=%d", len(self._bindings))
        message = wintypes.MSG()
        try:
            while not self.stop_event.is_set():
                while user32.PeekMessageW(
                    ctypes.byref(message), None, 0, 0, PM_REMOVE
                ):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                self.stop_event.wait(0.01)
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
            LOG.info("hotkey worker stopped")


__all__ = ["HotkeyWorker"]
