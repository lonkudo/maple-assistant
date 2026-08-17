"""Fixed channel-switch procedure: esc -> enter -> random left/down -> enter -> wait.

Reusable by the assistant UI button (Additional Functions panel) and the
standalone CLI test (work/channel_switch_test.py).  This is a FIXED
PROCEDURE - it is not a key binding and touches no UI binding state.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional

CHANNEL_KEY_DELAY = 0.20
CHANNEL_HOLD = 0.06
CHANNEL_WAIT = 3.0


def channel_switch_procedure(
    sender: Any,
    *,
    left_count: Optional[int] = None,
    down_count: Optional[int] = None,
    key_delay: float = CHANNEL_KEY_DELAY,
    hold: float = CHANNEL_HOLD,
    wait: float = CHANNEL_WAIT,
    on_press: Optional[Callable[[str, bool], None]] = None,
) -> bool:
    """Run the fixed channel switch; True when every key was sent.

    Sequence: esc -> enter -> left x N -> down x M -> enter -> wait
    ``wait`` seconds, with N/M random 1-10 unless overridden.  Keys are
    sent through ``sender.press`` (scan-code sender).  Returns False as
    soon as a key is blocked (e.g. the game lost focus mid-procedure).
    """

    left_count = (
        random.randint(1, 10) if left_count is None else int(left_count)
    )
    down_count = (
        random.randint(1, 10) if down_count is None else int(down_count)
    )
    keys = (
        ["esc", "enter"]
        + ["left"] * left_count
        + ["down"] * down_count
        + ["enter"]
    )
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
