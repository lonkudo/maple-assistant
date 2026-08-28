"""Non-blocking Windows MP3 playback for BOSS Tracker alarms."""

from __future__ import annotations

import ctypes
from pathlib import Path
import subprocess
import threading
import time
from typing import Iterable


POWERSHELL_TTS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($speaker.GetInstalledVoices() | Where-Object Enabled |
    ForEach-Object { $_.VoiceInfo })
$voice = $voices | Where-Object {
    $_.Culture.Name -like 'zh-*' -and $_.Gender -eq 'Female'
} | Select-Object -First 1
if ($null -eq $voice) {
    $voice = $voices | Where-Object { $_.Culture.Name -like 'zh-*' } |
        Select-Object -First 1
}
if ($null -eq $voice) {
    $voice = $voices | Where-Object { $_.Gender -eq 'Female' } |
        Select-Object -First 1
}
if ($null -ne $voice) { $speaker.SelectVoice($voice.Name) }
$speaker.Rate = -1
$speaker.Volume = 100
$speaker.Speak($args[0])
$speaker.Dispose()
"""


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


def speak_chinese(text: str) -> None:
    """Speak through the best installed female Chinese Windows voice."""

    if not text:
        return
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                POWERSHELL_TTS,
                text,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return


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
]
