"""Independent timed Ctrl attack worker.

This module does not import movement or status analysis. It owns no direction
keys and consumes no screenshots.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class AttackWorker(threading.Thread):
    """Tap the attack key on a monotonic timer, except during climb input.

    ``enabled`` (default True) can be flipped live from the UI: when False
    the timer keeps running but no key is ever sent.  ``attack_interval``
    and ``attack_key`` are plain attributes the UI can also update live.

    The fixed attack NEVER pauses the patrol walk: the character attacks
    while walking (normal MapleStory behavior).  Publishing an attack-active
    window here made the movement worker stop every attack - with a fast
    interval the character crawled and looked stuck at the platform edge.
    """

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        attack_interval: float = 3.0,
        *,
        attack_key: str = "ctrl",
        climbing_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        initial_offset: Optional[float] = None,
    ) -> None:
        super().__init__(name="attack-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.attack_interval = max(0.25, attack_interval)
        self.attack_key = str(attack_key).casefold()
        scan_map = getattr(key_sender, "_SCAN", None)
        if scan_map is not None and self.attack_key not in scan_map:
            LOG.warning("unsupported attack key %r; falling back to 'ctrl'",
                        attack_key)
            self.attack_key = "ctrl"
        self.enabled = True
        self.climbing_active_event = climbing_active_event
        self.automation_active_event = automation_active_event
        self.initial_offset = (
            self.attack_interval / 2.0
            if initial_offset is None else max(0.0, initial_offset)
        )

    def set_key(self, key: str) -> bool:
        """Validate + apply a new attack key; False when unsupported."""

        scan_map = getattr(self.key_sender, "_SCAN", None)
        key = str(key).casefold()
        if scan_map is not None and key not in scan_map:
            return False
        self.attack_key = key
        return True

    def attack_once(self) -> bool:
        """Send exactly the attack key down/up; never move the character."""

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is not None and key_up is not None:
            claimed = key_down(self.attack_key) is not False
            if not claimed:
                return False
            return key_up(self.attack_key) is not False
        return self.key_sender.tap(self.attack_key) is not False

    def run(self) -> None:
        LOG.info("attack worker started offset=%.3fs interval=%.3fs key=%s "
                 "enabled=%s",
                 self.initial_offset, self.attack_interval, self.attack_key,
                 self.enabled)
        next_attack = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_attack - time.monotonic())):
                break
            if not self.enabled:
                pass
            elif (self.automation_active_event is not None
                    and not self.automation_active_event.is_set()):
                pass
            elif (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                LOG.info("attack skipped: jump-climb input is active")
            else:
                LOG.info("attack repetition: %s", self.attack_key)
                self.attack_once()
            next_attack = time.monotonic() + self.attack_interval
        LOG.info("attack worker stopped")


__all__ = ["AttackWorker"]
