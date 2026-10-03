"""Trade accept-button position probe (run this on the machine that misses the button).

Why: the trade dialog button is anchored to the game client's BOTTOM-RIGHT corner, and the offset
is (206, 91) measured on a 1366x768 client.  A narrower client (1080x768) either shrinks that offset
proportionally or keeps it fixed, and only the machine itself can tell us which.

How to use (the game must be running):

    1.  <install>\\.venv\\Scripts\\python.exe trade_offset_probe.py
    2.  Bring the GAME to the front, open the trade window, and hover the mouse over the
        "accept" button.  Keep the cursor still for a second.
    3.  The probe prints one line per steady position, e.g.

        steady: cursor=(1085, 858) | client origin=(170, 161) size=1080x768
                bottom_right=(1250, 929) | offset_from_client_br=(165, 71)
                | click scaled=(1087, 857) fixed=(1044, 838)

        offset_from_client_br  is the number we need.
    4.  Alt+Tab back to this window and press Ctrl+C.  The summary tells you which file to share;
        add --write to save it as trade_offsets.json next to this script, which the assistant reads
        on its next start (accept_offset / accept_scale).

Only stable readings (cursor still for STILL_SECONDS) while the game window is in front are
reported, so moving the mouse back to the console cannot pollute the answer.

Output is ASCII only: a Windows console may use a non-UTF8 code page.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import time

STILL_SECONDS = 0.60
POLL_SECONDS = 0.05
STILL_PIXELS = 2
DEFAULT_TITLE = "冒险岛"
REFERENCE_OFFSET = (206, 91)
REFERENCE_WIDTH = 1366


def _cursor() -> tuple[int, int]:
    point = wintypes.POINT()
    if not ctypes.WinDLL("user32", use_last_error=True).GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return int(point.x), int(point.y)


def _find_game_window(substring: str) -> int:
    import win32gui

    wanted = substring.casefold()
    found: list[tuple[int, str]] = []

    def visit(hwnd: int, _extra: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title and wanted in title.casefold():
            found.append((hwnd, title))
        return True

    win32gui.EnumWindows(visit, None)
    if not found:
        return 0
    # The game's top-level window is the one with the largest client area.
    def client_area(item: tuple[int, str]) -> int:
        left, top, right, bottom = win32gui.GetClientRect(item[0])
        return (right - left) * (bottom - top)

    hwnd, title = max(found, key=client_area)
    print(f"game window: hwnd={hwnd} title={title.encode('ascii', 'backslashreplace').decode()}")
    return hwnd


def _geometry(hwnd: int) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    import win32gui

    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    _cr = win32gui.GetClientRect(hwnd)
    right, bottom = win32gui.ClientToScreen(hwnd, (_cr[2], _cr[3]))
    client = (left, top, right - left, bottom - top)
    window = win32gui.GetWindowRect(hwnd)
    return client, (int(window[0]), int(window[1]), int(window[2]), int(window[3]))


def _click_point(client: tuple[int, int, int, int], offset: tuple[int, int],
                 scale: float) -> tuple[int, int]:
    left, top, width, height = client
    return (left + width - round(offset[0] * scale),
            top + height - round(offset[1] * scale))


def main() -> int:
    # Print as we go: redirected output is block-buffered by default, which would hide the readings
    # until the process exits.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window-title", default=DEFAULT_TITLE,
                        help="substring of the game window title (default: the game's own title)")
    parser.add_argument("--write", action="store_true",
                        help="write the last steady reading to trade_offsets.json next to this file")
    parser.add_argument("--client", default="",
                        help="skip window discovery: use this WxH or x,y,w,h client geometry")
    args = parser.parse_args()

    hwnd = 0
    if not args.client:
        hwnd = _find_game_window(args.window_title)
        if not hwnd:
            print("no game window found - start the game, or pass --window-title")
            return 1

    import win32gui

    print("hover the mouse over the trade ACCEPT button and keep it still; Ctrl+C to finish")
    print(f"reference: 1366x768 client -> offset {REFERENCE_OFFSET} from the bottom-right corner")
    steady: tuple[int, int] | None = None
    steady_since = 0.0
    last_reported: tuple[int, int] | None = None
    readings: list[dict] = []

    while True:
        try:
            cursor = _cursor()
            foreground = int(win32gui.GetForegroundWindow())
            if args.client:
                parts = [int(value) for value in args.client.replace("x", ",").split(",")]
                client = tuple(parts) if len(parts) == 4 else (0, 0, parts[0], parts[1])
                window_rect = (0, 0)
            else:
                client, window_rect = _geometry(hwnd)
                foreground_ok = foreground == hwnd
                if not foreground_ok:
                    steady = None
                    time.sleep(POLL_SECONDS)
                    continue
            if steady is None or (abs(cursor[0] - steady[0]) > STILL_PIXELS
                                  or abs(cursor[1] - steady[1]) > STILL_PIXELS):
                steady = cursor
                steady_since = time.monotonic()
            elif (time.monotonic() - steady_since >= STILL_SECONDS
                  and cursor != last_reported):
                last_reported = cursor
                left, top, width, height = client
                offset = (left + width - cursor[0], top + height - cursor[1])
                factor = min(1.0, width / REFERENCE_WIDTH)
                scaled = _click_point(client, REFERENCE_OFFSET, factor)
                fixed = _click_point(client, REFERENCE_OFFSET, 1.0)
                print(
                    f"steady: cursor={cursor} | client origin=({left}, {top}) "
                    f"size={width}x{height} bottom_right=({left + width}, {top + height}) "
                    f"| offset_from_client_br={offset} | window_rect={window_rect}\n"
                    f"        click with scale=width({factor:.4f}) -> {scaled}"
                    f" | scale=none -> {fixed}"
                )
                readings.append({
                    "cursor": cursor,
                    "client": list(client),
                    "window_rect": list(window_rect),
                    "offset_from_client_br": list(offset),
                    "scale_width_point": list(scaled),
                    "scale_none_point": list(fixed),
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as exc:  # keep the probe alive through transient failures
            print(f"probe error: {exc!r}")
            time.sleep(0.5)

    print("")
    if not readings:
        print("no steady reading was recorded - hover over the button and keep the cursor still")
        return 1
    print(f"{len(readings)} steady reading(s); the last five:")
    for item in readings[-5:]:
        print(f"  {item['at']}  cursor={tuple(item['cursor'])}  "
              f"offset_from_client_br={tuple(item['offset_from_client_br'])}")
    answer = tuple(readings[-1]["offset_from_client_br"])
    print(f"\noffset from the client's bottom-right corner: {answer}")
    if args.write:
        target = Path(__file__).resolve().with_name("trade_offsets.json")
        payload = {
            "accept_offset": [int(answer[0]), int(answer[1])],
            # The probe measured the real pixel distance, so it is used as-is.
            "accept_scale": "none",
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"written: {target}")
        print("restart the assistant to apply it")
    else:
        print("re-run with --write to save it as trade_offsets.json, or send this line back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
