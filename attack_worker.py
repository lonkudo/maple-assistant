"""Independent timed Ctrl attack worker.

This module does not import movement or status analysis. It owns no direction
keys and consumes no screenshots.
"""

from __future__ import annotations

import logging
import random
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
        attack_jitter_seconds: float = 0.1,
        motion_arbiter: Any = None,
        jump_attack: bool = False,
        jump_attack_delay: float = 0.3,
    ) -> None:
        super().__init__(name="attack-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.attack_interval = max(0.2, attack_interval)
        # 下一次攻击 = configured interval + UI-configurable random gap.
        self.attack_jitter_seconds = max(0.0, float(attack_jitter_seconds))
        self.attack_key = str(attack_key).casefold()
        scan_map = getattr(key_sender, "_SCAN", None)
        if scan_map is not None and self.attack_key not in scan_map:
            LOG.warning("unsupported attack key %r; falling back to 'ctrl'",
                        attack_key)
            self.attack_key = "ctrl"
        self.enabled = True
        self.climbing_active_event = climbing_active_event
        self.automation_active_event = automation_active_event
        # Optional MotionArbiter: while jump/buff events are queued or
        # executing (their action motion is playing) the attack is deferred
        # until the arbiter is idle, so attack motion cannot swallow them.
        self.motion_arbiter = motion_arbiter
        # 跳跃攻击 mode: every cadence beat emits a bundle - jump (Alt) first,
        # then the attack key ``jump_attack_delay`` (default 0.3s) later.  The
        # jump belongs to the attack bundle in this mode, so NO jump event is
        # registered into the motion-arbiter queue.
        self.jump_attack = bool(jump_attack)
        self.jump_attack_delay = max(0.0, float(jump_attack_delay))
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
        """Send a game-recognizable short attack press; never move.

        A zero-duration down/up pair is visible in the debug log but can be
        ignored by MapleStory between input polls.  Prefer the key sender's
        normal ``tap`` method, which holds the scan code for 25 ms just like
        the reliable potion input path.

        In 跳跃攻击 mode the beat is a bundle: tap the jump key (Alt), wait
        ``jump_attack_delay`` (300ms), then tap the configured attack key.
        """

        if self.jump_attack:
            tap = getattr(self.key_sender, "tap", None)
            if not callable(tap):
                return False
            if tap("alt") is False:
                return False
            if self.stop_event.wait(self.jump_attack_delay):
                # Stopping mid-bundle: skip the attack half.
                return False
            return tap(self.attack_key) is not False

        tap = getattr(self.key_sender, "tap", None)
        if callable(tap):
            return tap(self.attack_key) is not False

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is not None and key_up is not None:
            claimed = key_down(self.attack_key) is not False
            if not claimed:
                return False
            return key_up(self.attack_key) is not False
        return False

    def next_delay(self) -> float:
        """Configured attack interval plus the configured random gap."""

        random_gap = random.uniform(0.0, self.attack_jitter_seconds)
        return max(0.05, self.attack_interval + random_gap)

    def run(self) -> None:
        LOG.info("attack worker started offset=%.3fs interval=%.3fs key=%s "
                 "enabled=%s",
                 self.initial_offset, self.attack_interval, self.attack_key,
                 self.enabled)
        next_attack = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            if self.stop_event.wait(max(0.0, next_attack - time.monotonic())):
                break
            can_fire = self.enabled
            attack_lease = False
            if can_fire and (self.automation_active_event is not None
                    and not self.automation_active_event.is_set()):
                can_fire = False
            if can_fire and (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                LOG.info("attack skipped: climb/return input is active")
                can_fire = False
            if can_fire and self.motion_arbiter is not None:
                # Reservation is deliberately atomic.  Checking idle and
                # tapping separately allowed a queued 小碎步 to start between
                # those operations, so a directional sequence could overlap
                # the attack animation.  One lease owns this whole tap.
                attack_lease = self.motion_arbiter.try_begin_attack()
                if not attack_lease:
                    self.motion_arbiter.wait_until_idle()
                    attack_lease = bool(
                        self.enabled
                        and self.motion_arbiter.try_begin_attack()
                    )
                can_fire = attack_lease
                if can_fire and (self.automation_active_event is not None
                        and not self.automation_active_event.is_set()):
                    can_fire = False
                if can_fire and (self.climbing_active_event is not None
                        and self.climbing_active_event.is_set()):
                    LOG.info("attack skipped: climb/return input is active")
                    can_fire = False
            if not can_fire and attack_lease and self.motion_arbiter is not None:
                # A Stop/climb edge may happen directly after reservation.
                # Always return it, otherwise motion input would stay blocked.
                self.motion_arbiter.finish_attack(False)
                attack_lease = False
            if can_fire:
                LOG.info("attack repetition: %s", self.attack_key)
                sent = False
                try:
                    sent = self.attack_once()
                finally:
                    if self.motion_arbiter is not None and attack_lease:
                        self.motion_arbiter.finish_attack(sent)
            # The random component is additive only: attack + random_gap.
            next_attack = time.monotonic() + self.next_delay()
        LOG.info("attack worker stopped")


__all__ = ["AttackWorker"]
