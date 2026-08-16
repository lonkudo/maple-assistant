#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Key delivery probe: figure out how the game accepts keyboard input.

Presses Ctrl (and optionally Left) several times using three methods:
  1. SendInput scan-code (current attacker method)
  2. SendInput virtual-key
  3. PostMessage WM_KEYDOWN to the game window (direct delivery)

Run with the GAME WINDOW FOCUSED and watch whether the character attacks
(and turns left) on each method.  Each method fires 3 times, 2s apart.

Usage:
    python key_probe.py [--window-title 冒险岛怀旧服] [--rounds 3]
"""

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attack_executor import _send_scan_code, _SCAN

# Virtual key codes (for method 2 and 3).
VK = {
    "ctrl": 0x11,
    "left": 0x25,
    "right": 0x27,
    "alt": 0x12,
}

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


def send_virtual_key(key: str, down: bool) -> None:
    """SendInput with virtual-key codes (no scan code)."""

    flags = 0
    if not down:
        flags |= 0x0002  # KEYEVENTF_KEYUP
    inp_type = 1
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT)]
    inp = INPUT()
    inp.type = inp_type
    inp.ki = KEYBDINPUT(VK[key], 0, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def find_window(title: str) -> int:
    import win32gui

    matches = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if title.casefold() in win32gui.GetWindowText(hwnd).casefold():
                matches.append(hwnd)

    win32gui.EnumWindows(collect, None)
    return matches[0] if matches else 0


def post_message_key(hwnd: int, key: str, down: bool) -> None:
    """Deliver WM_KEYDOWN/UP straight to the window's message queue."""

    msg = WM_KEYDOWN if down else WM_KEYUP
    ctypes.windll.user32.PostMessageW(hwnd, msg, VK[key], 0)


def press(method: int, hwnd: int, key: str, hold: float = 0.12) -> None:
    print(f"  method {method} key={key} down...", flush=True)
    if method == 1:
        scan, ext = _SCAN[key]
        _send_scan_code(scan, key_up=False, extended=ext)
        time.sleep(hold)
        _send_scan_code(scan, key_up=True, extended=ext)
    elif method == 2:
        send_virtual_key(key, True)
        time.sleep(hold)
        send_virtual_key(key, False)
    elif method == 3:
        if not hwnd:
            print("    no game window - skip", flush=True)
            return
        post_message_key(hwnd, key, True)
        time.sleep(hold)
        post_message_key(hwnd, key, False)
    print(f"  method {method} key={key} UP", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-title", default="\u5192\u9669\u5c9b\u6000\u65e7\u670d")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    print(f"=== KEY PROBE title={args.window_title!r} rounds={args.rounds} ===")
    print("FOCUS THE GAME WINDOW NOW - watch the character!")
    time.sleep(3)
    hwnd = find_window(args.window_title)
    print(f"game window hwnd={hwnd}")

    for method in (1, 2, 3):
        print(f"--- METHOD {method}: "
              f"{'SendInput scan-code' if method == 1 else 'SendInput VK' if method == 2 else 'PostMessage WM_KEYDOWN'}")
        for i in range(args.rounds):
            press(method, hwnd, "ctrl")
            time.sleep(1.0)
            press(method, hwnd, "left")
            time.sleep(2.0)
    print("=== PROBE DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
