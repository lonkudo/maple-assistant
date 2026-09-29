"""Input-only channel switch procedure.

Reusable by the assistant UI button (Additional Functions panel) and the
standalone CLI test (work/channel_switch_test.py).  This is a FIXED
PROCEDURE - it is not a key binding and touches no UI binding state.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

CHANNEL_KEY_DELAY = 0.20
CHANNEL_HOLD = 0.06
CHANNEL_WAIT = 3.0


def channel_switch_procedure(
    sender: Any,
    *,
    moves: Optional[tuple[tuple[str, int], ...]] = None,
    left_count: Optional[int] = None,
    down_count: Optional[int] = None,
    key_delay: float = CHANNEL_KEY_DELAY,
    hold: float = CHANNEL_HOLD,
    wait: float = CHANNEL_WAIT,
    on_press: Optional[Callable[[str, bool], None]] = None,
) -> bool:
    """Run a planned channel switch; True when every key was sent.

    ``moves`` is the route planner's ordered direction/count pairs.  The old
    optional counts remain only for callers outside the new planner.  Keys are
    sent through ``sender.press`` and the procedure stops at the first refusal.
    """

    if moves is None:
        legacy_left = max(0, int(left_count or 0))
        legacy_down = max(0, int(down_count or 0))
        moves = (("left", legacy_left), ("down", legacy_down))
    keys = ["esc", "enter"]
    for key, count in moves:
        if key not in {"left", "right", "up", "down"}:
            raise ValueError(f"unsupported channel route key: {key}")
        keys.extend([key] * max(0, int(count)))
    keys.extend(["enter", "esc"])
    for key in keys:
        ok = bool(sender.press(key, duration=hold))
        if on_press is not None:
            on_press(key, ok)
        if not ok:
            return False
        time.sleep(key_delay)
    time.sleep(wait)
    return True


__all__ = ["channel_switch_procedure"]
