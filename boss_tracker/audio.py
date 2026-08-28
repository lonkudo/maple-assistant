"""Non-blocking Windows MP3 playback for BOSS Tracker alarms."""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterable


VENDOR_DIR = Path(__file__).resolve().with_name("vendor")
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))


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


def number_to_chinese(number: int) -> str:
    """Convert a channel number in the supported 1..60 range to Chinese."""

    number = int(number)
    if not 1 <= number <= 60:
        raise ValueError("channel number must be from 1 to 60")
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    tens, ones = divmod(number, 10)
    prefix = "十" if tens == 1 else digits[tens] + "十"
    return prefix + (digits[ones] if ones else "")


def channel_announcement(channel_names: Iterable[str]) -> str:
    """Build a phrase that reads every channel number exactly twice."""

    phrases: list[str] = []
    for name in channel_names:
        spoken = number_to_chinese(int(str(name).strip()))
        phrase = f"频道{spoken}"
        phrases.extend((phrase, phrase))
    return "，".join(phrases)


def select_female_chinese_voice(voices: Iterable[Any]) -> Any | None:
    """Prefer female zh-CN, then any Chinese, then any female voice."""

    tokens = list(voices)

    def attribute(token: Any, name: str) -> str:
        try:
            return str(token.GetAttribute(name)).lower()
        except Exception:
            return ""

    for token in tokens:
        language = attribute(token, "Language").lstrip("0")
        if language == "804" and attribute(token, "Gender") == "female":
            return token
    for token in tokens:
        if attribute(token, "Language").lstrip("0") == "804":
            return token
    for token in tokens:
        if attribute(token, "Gender") == "female":
            return token
    return tokens[0] if tokens else None


def speak_chinese(text: str) -> bool:
    """Speak directly through SAPI using the bundled lightweight comtypes."""

    if not text:
        return False
    try:
        import comtypes
        from comtypes.client import CreateObject
    except (ImportError, OSError):
        return False
    comtypes.CoInitialize()
    try:
        speaker = CreateObject("SAPI.SpVoice")
        collection = speaker.GetVoices()
        voices = [collection.Item(index) for index in range(collection.Count)]
        voice = select_female_chinese_voice(voices)
        if voice is not None:
            speaker.Voice = voice
        speaker.Rate = -1
        speaker.Volume = 100
        speaker.Speak(text)
        return True
    except Exception:
        return False
    finally:
        comtypes.CoUninitialize()


def announce_channels_async(path: Path, channel_names: Iterable[str]) -> None:
    """Play the alarm, then announce each expired channel twice."""

    names = tuple(str(name) for name in channel_names)

    def announce() -> None:
        play_mp3(path)
        try:
            speak_chinese(channel_announcement(names))
        except (TypeError, ValueError):
            return

    threading.Thread(
        target=announce,
        name="boss-announcement",
        daemon=True,
    ).start()


__all__ = [
    "announce_channels_async",
    "channel_announcement",
    "number_to_chinese",
    "play_mp3_async",
    "select_female_chinese_voice",
]
