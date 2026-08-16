#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto-attack executor for the YOLO live view.

Faces the chosen target with a brief left/right tap, then presses the
attack key (Ctrl by default) using native SendInput scan-code events.
Keys are only sent while the game window is the foreground window, so the
bot never types into the UI or other applications.

When the game is not focused the executor tries to restore and refocus the
game window (rate-limited), so auto-attack recovers on its own after the
user clicks elsewhere.

Run with ``--attack`` in live_view.py::

    python live_view.py --attack [--attack-key ctrl] [--attack-log attack.log]
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("attack_executor")

# Set-1 keyboard scan codes.  Extended keys (arrows) need the E0 flag.
_SCAN = {
    "ctrl": (0x1D, False),
    "alt": (0x38, False),
    "left": (0x4B, True),
    "right": (0x4D, True),
}

ULONG_PTR = wintypes.WPARAM


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    # All members are required: the union size must match Win32 INPUT
    # (40 bytes on 64-bit Windows), even when only `ki` is used.
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008


def _send_scan_code(scan_code: int, key_up: bool, extended: bool) -> None:
    """Inject one hardware-like keyboard transition with Win32 SendInput.

    Byte-for-byte the same implementation as the assistant's
    ``WindowKeySender`` (status_worker.py), which is known to deliver keys
    to the game.  Raises OSError when SendInput injects fewer events than
    requested so callers never log a fake "attack" on a failed injection.
    """

    flags = _KEYEVENTF_SCANCODE
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    event = _INPUT(type=1, ki=_KEYBDINPUT(0, scan_code, flags, 0, 0))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (
        wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int
    )
    user32.SendInput.restype = wintypes.UINT
    ctypes.set_last_error(0)
    sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
    if sent != 1:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, f"SendInput injected {sent}/1 events")


class AttackExecutor:
    """Face the target and press the attack key while the game is focused.

    ``attack()`` is rate-limited by ``cooldown`` seconds and only emits keys
    when the configured game window is the foreground window.  Direction
    taps are sent only when the required facing changes, so the character
    turns without walking in tiny steps.  When the game is not focused the
    executor tries to restore + refocus it (at most once per
    ``refocus_interval`` seconds).
    """

    def __init__(
        self,
        window_title: str,
        *,
        attack_key: str = "ctrl",
        face_hold: float = 0.08,
        attack_hold: float = 0.1,
        cooldown: float = 0.6,
        refocus_interval: float = 3.0,
        turn_settle: float = 0.15,
        patrol_state_path: Optional[str] = None,
        log_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.window_title = window_title
        self.attack_key = attack_key.casefold()
        if self.attack_key not in _SCAN:
            raise ValueError(f"unsupported attack key: {attack_key}")
        self.face_hold = max(0.01, float(face_hold))
        self.attack_hold = max(0.01, float(attack_hold))
        self.cooldown = max(0.0, float(cooldown))
        self.refocus_interval = max(1.0, float(refocus_interval))
        self.turn_settle = max(0.0, float(turn_settle))
        self.dry_run = dry_run
        self._facing: Optional[str] = None  # last direction we turned toward
        self._last_attack = 0.0
        self._last_refocus = 0.0
        # Cross-process patrol state: when the patrol worker reports the
        # character is climbing/dropping, attacks are blocked so the attack
        # keys never fight the climb.
        self._patrol_state = None
        if patrol_state_path:
            try:
                from combat_coordination import PatrolStateFile

                self._patrol_state = PatrolStateFile(patrol_state_path)
            except Exception:
                LOG.warning("patrol state unavailable; attacks not gated",
                            exc_info=True)
        if log_path:
            self._enable_file_log(log_path)

    # ---- public API -------------------------------------------------

    def _enable_file_log(self, path: str) -> None:
        """Append DEBUG logs (including block reasons) to *path*."""

        handler = logging.FileHandler(Path(path), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        LOG.addHandler(handler)
        LOG.setLevel(logging.DEBUG)
        LOG.info("attack executor logging to %s", path)

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
            if self.window_title.casefold() in title.casefold():
                self._foreground_title = title
                return True
            LOG.debug("foreground window title=%r does not match %r",
                      title, self.window_title)
            return False
        except Exception:  # pywin32 absent or no desktop session
            LOG.warning("cannot verify foreground window", exc_info=True)
            return False

    def select_window(self) -> bool:
        """Restore and refocus the game window (best effort).

        Finds the game window by title; unminimizes it and brings it to the
        foreground.  Plain SetForegroundWindow is often refused by the
        Windows foreground lock, so it falls back to an Alt transition and
        thread-input attachment (same chain as the assistant's
        WindowKeySender).  Returns True when the game now has focus.
        """

        if self.dry_run:
            return True
        try:
            import win32api
            import win32con
            import win32gui
            import win32process

            matches: list[int] = []

            def collect(hwnd: int, _extra: object) -> None:
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if self.window_title.casefold() in title.casefold():
                        matches.append(hwnd)

            win32gui.EnumWindows(collect, None)
            if not matches:
                LOG.warning("refocus: no game window matching %r found",
                            self.window_title)
                return False
            hwnd = matches[0]
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.05)

            if win32gui.GetForegroundWindow() == hwnd:
                return True
            # 1) Direct attempt (usually succeeds when our process was the
            #    last to receive input).
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                LOG.debug("direct foreground selection refused", exc_info=True)
            time.sleep(0.05)
            if win32gui.GetForegroundWindow() == hwnd:
                return True

            # 2) Alt transition: briefly press/release Alt, which lets
            #    Windows accept a foreground request after its lock timeout.
            _send_scan_code(0x38, key_up=False, extended=False)
            _send_scan_code(0x38, key_up=True, extended=False)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                LOG.debug("Alt-assisted foreground selection refused",
                          exc_info=True)
            time.sleep(0.05)
            if win32gui.GetForegroundWindow() == hwnd:
                return True

            # 3) Thread-input attachment: final best-effort fallback.
            foreground = win32gui.GetForegroundWindow()
            if foreground:
                current_tid = win32api.GetCurrentThreadId()
                foreground_tid = win32process.GetWindowThreadProcessId(
                    foreground)[0]
                target_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
                attached: list[int] = []
                try:
                    for thread_id in {foreground_tid, target_tid}:
                        if thread_id and thread_id != current_tid:
                            try:
                                win32process.AttachThreadInput(
                                    current_tid, thread_id, True
                                )
                                attached.append(thread_id)
                            except Exception:
                                LOG.debug("could not attach input thread %s",
                                          thread_id, exc_info=True)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        LOG.debug("attached foreground selection refused",
                                  exc_info=True)
                    time.sleep(0.05)
                finally:
                    for thread_id in reversed(attached):
                        try:
                            win32process.AttachThreadInput(
                                current_tid, thread_id, False
                            )
                        except Exception:
                            pass
            return win32gui.GetForegroundWindow() == hwnd
        except Exception as exc:
            LOG.warning("refocus failed: %s", exc)
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
        cooldown window return False without sending anything.  If the game
        is not focused, tries to refocus it (rate-limited) before giving up.
        """

        now = time.monotonic()
        if now - self._last_attack < self.cooldown:
            return False
        # Block attacks while patrol is climbing/dropping: the character is
        # on a rope or mid-drop, and an attack key would fight the climb.
        if self._patrol_state is not None and self._patrol_state.is_busy():
            LOG.debug("attack blocked: patrol climbing/dropping")
            return False
        if not self.is_game_foreground():
            if now - self._last_refocus >= self.refocus_interval:
                self._last_refocus = now
                if not self.select_window():
                    LOG.debug("attack blocked: game not foreground "
                              "(refocus failed)")
                    return False
            else:
                LOG.debug("attack blocked: game not foreground (waiting to "
                          "refocus)")
                return False

        facing = self.facing_for(character, target)
        try:
            # Always turn toward the target before attacking.  A short
            # left/right tap only turns the character (it does not walk), so
            # re-tapping every attack is harmless when already facing the
            # right way and self-corrects any belief desync.  Never trust a
            # cached facing to skip the turn - the game is the source of
            # truth.
            if facing is not None:
                self._tap(facing, self.face_hold)
                self._facing = facing
                # Let the game register the turn before the attack lands so
                # the character actually faces the monster.
                time.sleep(self.turn_settle)
            self._tap(self.attack_key, self.attack_hold)
        except OSError as exc:
            LOG.error("attack FAILED: %s", exc)
            return False
        self._last_attack = now
        LOG.info("attack: key=%s facing=%s player=(%.0f,%.0f) "
                 "target=(%.0f,%.0f) dx=%.0f dy=%.0f",
                 self.attack_key, self._facing,
                 character.center[0], character.center[1],
                 target.center[0], target.center[1],
                 target.center[0] - character.center[0],
                 target.center[1] - character.center[1])
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
