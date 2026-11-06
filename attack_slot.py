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

        key, _replaced = self.consume_with_source(default_key)
        return key

    def consume_with_source(self, default_key: Optional[str]) -> tuple[Optional[str], bool]:
        """Return ``(key, replaced)`` while atomically clearing the one-shot slot.

        The ordinary :meth:`consume` API intentionally remains available to
        movement corrections.  The cadence attack worker additionally needs
        to know whether this beat sent its configured combo key or a
        counterattack replacement: only the former may advance a combo run.
        """

        with self._lock:
            key = self._replacement
            self._replacement = None
        return (key if key is not None else default_key, key is not None)

    def clear(self) -> bool:
        """Discard an unspent replacement when its feature is disabled."""

        with self._lock:
            had_replacement = self._replacement is not None
            self._replacement = None
        return had_replacement


__all__ = ["AttackSlot"]
