"""Thread-safe sequence planner for the optional 组合攻击 mode.

This module only decides which attack key belongs to the next eligible attack
beat.  It has no dependency on UI, movement, capture, or key delivery, so the
normal AttackWorker remains the sole owner of timing and input arbitration.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import threading
from typing import Iterable, Optional


@dataclass(frozen=True)
class ComboAttackSlot:
    """One consecutive run in a combo attack sequence."""

    minimum: int
    maximum: int
    key: Optional[str]

    @classmethod
    def from_value(cls, value: object) -> "ComboAttackSlot":
        """Make a safe slot from a persisted/UI mapping."""

        data = value if isinstance(value, dict) else {}
        try:
            minimum = int(data.get("min_count", 1))
        except (TypeError, ValueError):
            minimum = 1
        try:
            maximum = int(data.get("max_count", minimum))
        except (TypeError, ValueError):
            maximum = minimum
        minimum = max(0, min(999, minimum))
        maximum = max(0, min(999, maximum))
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        raw_key = str(data.get("key", "-")).strip().casefold()
        key = None if raw_key in ("", "-", "none", "null") else raw_key
        return cls(minimum, maximum, key)


class ComboAttackPlan:
    """Advance three optional key runs without coupling to the attack clock.

    A key is returned once for every eligible attack beat.  A ``None`` key is
    a deliberate idle beat: the surrounding AttackWorker keeps its cadence,
    but does not send an input.  Equal bounds never call the random generator.
    """

    SLOT_COUNT = 3

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._slots = tuple(
            ComboAttackSlot(1, 1, None) for _ in range(self.SLOT_COUNT)
        )
        self._slot_index = 0
        self._remaining = 0

    def configure(self, enabled: bool, slots: Iterable[object]) -> None:
        normalized = [ComboAttackSlot.from_value(slot) for slot in slots]
        normalized = normalized[:self.SLOT_COUNT]
        while len(normalized) < self.SLOT_COUNT:
            normalized.append(ComboAttackSlot(1, 1, None))
        with self._lock:
            normalized_slots = tuple(normalized)
            if (self._enabled == bool(enabled)
                    and self._slots == normalized_slots):
                return
            self._enabled = bool(enabled)
            self._slots = normalized_slots
            self._slot_index = 0
            self._remaining = 0

    def peek_key(self, default_key: str) -> Optional[str]:
        """Return the current key without consuming its configured count."""

        with self._lock:
            if not self._enabled:
                return default_key
            if self._remaining > 0:
                return self._slots[self._slot_index].key
            # A zero-count run is disabled for this pass.  Walk forward until
            # one run samples a positive count; if every run is zero, return
            # a safe idle beat without inventing an attack.
            for _ in range(len(self._slots)):
                slot = self._slots[self._slot_index]
                # Identical bounds are intentionally deterministic.
                self._remaining = (
                    slot.minimum if slot.minimum == slot.maximum
                    else random.randint(slot.minimum, slot.maximum)
                )
                if self._remaining > 0:
                    return slot.key
                self._slot_index = (self._slot_index + 1) % len(self._slots)
            return None

    def consume(self) -> None:
        """Consume one approved beat after the attack worker accepts it."""

        with self._lock:
            if not self._enabled:
                return
            if self._remaining <= 0:
                return
            self._remaining -= 1
            if self._remaining <= 0:
                self._slot_index = (self._slot_index + 1) % len(self._slots)


__all__ = ["ComboAttackPlan", "ComboAttackSlot"]
