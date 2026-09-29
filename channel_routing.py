"""Pure planning for the in-game 6 x 10 channel selector.

This module intentionally knows neither UI widgets nor key delivery.  It only
turns the selected current channel plus an optional room code into a different
destination and the directional route to reach it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random


CHANNEL_MIN = 1
CHANNEL_MAX = 60
CHANNELS_PER_ROW = 6


@dataclass(frozen=True)
class ChannelRoute:
    """One planned movement inside the channel selector."""

    current: int
    target: int
    moves: tuple[tuple[str, int], ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(key for key, count in self.moves for _ in range(count))


def normalize_channel(value: object, default: int = CHANNEL_MIN) -> int:
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return default
    return channel if CHANNEL_MIN <= channel <= CHANNEL_MAX else default


def room_code_cycle(room_code: object) -> tuple[int, ...]:
    """Return the stable, full 60-channel permutation for one room code."""

    seed = hashlib.sha256(str(room_code).strip().encode("utf-8")).digest()
    channels = list(range(CHANNEL_MIN, CHANNEL_MAX + 1))
    random.Random(seed).shuffle(channels)
    return tuple(channels)


def choose_next_channel(current: object, room_code: object = "") -> int:
    """Choose a different destination; coded routes cover all channels once."""

    current_channel = normalize_channel(current)
    code = str(room_code or "").strip()
    if not code:
        choices = [value for value in range(CHANNEL_MIN, CHANNEL_MAX + 1)
                   if value != current_channel]
        return random.SystemRandom().choice(choices)
    cycle = room_code_cycle(code)
    return cycle[(cycle.index(current_channel) + 1) % len(cycle)]


def plan_channel_route(current: object, target: object) -> ChannelRoute:
    """Plan signed left/right/up/down moves for the 6-column selector."""

    source = normalize_channel(current)
    destination = normalize_channel(target)
    if destination == source:
        raise ValueError("channel target must differ from current channel")
    source_index = source - CHANNEL_MIN
    destination_index = destination - CHANNEL_MIN
    row_delta = destination_index // CHANNELS_PER_ROW - source_index // CHANNELS_PER_ROW
    column_delta = destination_index % CHANNELS_PER_ROW - source_index % CHANNELS_PER_ROW
    moves: list[tuple[str, int]] = []
    if column_delta:
        moves.append(("right" if column_delta > 0 else "left", abs(column_delta)))
    if row_delta:
        moves.append(("down" if row_delta > 0 else "up", abs(row_delta)))
    return ChannelRoute(source, destination, tuple(moves))


def plan_next_channel(current: object, room_code: object = "") -> ChannelRoute:
    """Compose destination selection and grid navigation."""

    source = normalize_channel(current)
    return plan_channel_route(source, choose_next_channel(source, room_code))


__all__ = [
    "CHANNEL_MAX", "CHANNEL_MIN", "CHANNELS_PER_ROW", "ChannelRoute",
    "choose_next_channel", "normalize_channel", "plan_channel_route",
    "plan_next_channel", "room_code_cycle",
]
