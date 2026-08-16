#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto-attack executor for the YOLO live view.

Faces the chosen target with a brief left/right tap, then presses the
attack key (Ctrl) using native SendInput scan-code events.  Keys are only
sent while the game window is the foreground window, so the bot never
types into the UI or other applications.

Run with ``--attack`` in live_view.py::

    python live_view.py --attack [--attack-cooldown 0.6] [--window-title ...]
"""

from __future__ import annotations

import ctypes
import logging
import time
from typing import Optional

LOG = logging.getLogger("attack_executor")

# Set-1 keyboard scan codes.  Extended keys (arrows) need the E0 flag.
_SCAN = {
    "ctrl": (0x1D, False),
    "left": (0x4B, True),
    "right": (0x4D, True),
}

# KEYEVENTF_* flags for SendInput.
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ki", _KEYBDINPUT)]


def _send_scan_code(scan_code: int, key_up: bool, extended: bool) -> None:
    """Inject one keyboard event via SendInput (no pywin32 needed)."""

    flags = _KEYEVENTF_SCANCODE
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(0, scan_code, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class AttackExecutor:
    """Face the target and press the attack key while the game is focused.

    ``attack()`` is rate-limited by ``cooldown`` seconds and only emits keys
    when the configured game window is the foreground window.  Direction
    taps are sent only when the required facing changes, so the character
    turns without walking in tiny steps.
    """

    def __init__(
        self,
        window_title: str,
        *,
        attack_key: str = "ctrl",
        face_hold: float = 0.05,
        attack_hold: float = 0.08,
        cooldown: float = 0.6,
        dry_run: bool = False,
    ) -> None:
        self.window_title = window_title
        self.attack_key = attack_key.casefold()
        if self.attack_key not in _SCAN:
            raise ValueError(f"unsupported attack key: {attack_key}")
        self.face_hold = max(0.01, float(face_hold))
        self.attack_hold = max(0.01, float(attack_hold))
        self.cooldown = max(0.0, float(cooldown))
        self.dry_run = dry_run
        self._facing: Optional[str] = None  # last direction we turned toward
        self._last_attack = 0.0

    # ---- public API -------------------------------------------------

    def is_game_foreground(self) -> bool:
        """True when the configured game window has keyboard focus."""

        if self.dry_run:
            return True
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False
            title = win32gui.GetWindowText(hwnd)
            return self.window_title.casefold() in title.casefold()
        except Exception:  # pywin32 absent or no desktop session
            LOG.warning("cannot verify foreground window", exc_info=True)
            return False

    def facing_for(
        self, character: object, target: object
    ) -> Optional[str]:
        """Facing direction that puts the character toward the target.

        Returns ``"left"``/``"right"``, or None when the target is directly
        above/below (keep the current facing).
        """

        dx = target.center[0] - character.center[0]
        if abs(dx) < 8:  # dead zone: roughly same x, facing doesn't matter
            return None
        return "right" if dx > 0 else "left"

    def attack(self, character: object, target: object) -> bool:
        """Face the target (if needed) and press the attack key.

        Returns True when an attack was emitted this call.  Calls inside the
        cooldown window return False without sending anything.
        """

        now = time.monotonic()
        if now - self._last_attack < self.cooldown:
            return False
        if not self.is_game_foreground():
            LOG.debug("attack blocked: game window is not foreground")
            return False

        facing = self.facing_for(character, target)
        if facing is not None and facing != self._facing:
            self._tap(facing, self.face_hold)
            self._facing = facing
        self._tap(self.attack_key, self.attack_hold)
        self._last_attack = now
        return True

    def reset_facing(self) -> None:
        """Forget the last facing direction (e.g. after a respawn)."""

        self._facing = None

    # ---- internals --------------------------------------------------

    def _tap(self, key: str, hold: float) -> None:
        if self.dry_run:
            LOG.info("DRY-RUN tap key=%s hold=%.3fs", key, hold)
            return
        scan_code, extended = _SCAN[key]
        _send_scan_code(scan_code, key_up=False, extended=extended)
        time.sleep(hold)
        _send_scan_code(scan_code, key_up=True, extended=extended)
