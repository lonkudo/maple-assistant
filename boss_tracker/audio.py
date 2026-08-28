"""Non-blocking Windows MP3 playback for BOSS Tracker alarms."""

from __future__ import annotations

import ctypes
from pathlib import Path
import threading
import time


def play_mp3(path: Path) -> None:
    path = Path(path).resolve()
    if not path.is_file():
        return
    try:
        winmm = ctypes.windll.winmm
    except (AttributeError, OSError):
        return
    alias = f"boss_tracker_{threading.get_ident()}_{time.monotonic_ns()}"

    def send(command: str) -> int:
        return int(winmm.mciSendStringW(command, None, 0, None))

    opened = False
    try:
        if send(f'open "{path}" type mpegvideo alias {alias}'):
            return
        opened = True
        send(f"play {alias} wait")
    finally:
        if opened:
            send(f"close {alias}")


def play_mp3_async(path: Path) -> None:
    threading.Thread(
        target=play_mp3,
        args=(Path(path),),
        name="boss-alarm",
        daemon=True,
    ).start()


__all__ = ["play_mp3_async"]
