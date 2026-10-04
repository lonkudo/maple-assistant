"""One-use replacement for the next approved attack beat.

The slot is deliberately independent of movement, status, and the cadence
worker.  A hit can replace one normal attack without creating a second attack
transaction that competes with a stationary correction.
"""

from __future__ import annotations

import threading
from typing import Optional


class AttackSlot:
    """Thread-safe, single pending attack-key replacement."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._replacement: Optional[str] = None

    def replace_next(self, key: str) -> bool:
        """Replace the next attack beat; retain an earlier unconsumed hit."""

        value = str(key).strip().casefold()
        if not value or value in ("-", "none", "null"):
            return False
        with self._lock:
            if self._replacement is None:
                self._replacement = value
        return True

    def consume(self, default_key: Optional[str]) -> Optional[str]:
        """Return and clear a replacement, otherwise return the normal key."""

        with self._lock:
            key = self._replacement
            self._replacement = None
        return key if key is not None else default_key


__all__ = ["AttackSlot"]
