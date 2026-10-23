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

from atomic_motion import AtomicMotionRunner
from combo_attack import ComboAttackPlan


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
        direction_transition_event: Optional[threading.Event] = None,
        jump_attack: bool = False,
        jump_attack_delay: float = 0.155,
        attack_resume_event: Optional[threading.Event] = None,
        attack_slot: Any = None,
        atomic_motion_runner: Optional[AtomicMotionRunner] = None,
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
        # A patrol endpoint reversal reserves a brief neutral/settle window.
        # Fixed attacks inside that window can make Maple ignore the newly
        # pressed opposite direction.
        self.direction_transition_event = direction_transition_event
        # 跳跃攻击 mode: every cadence beat emits a bundle - jump (Alt) first,
        # then the attack key ``jump_attack_delay`` (default 155ms) later. The
        # jump belongs to the attack bundle in this mode, so NO jump event is
        # registered into the motion-arbiter queue.
        self.jump_attack = bool(jump_attack)
        self.jump_attack_delay = max(0.0, float(jump_attack_delay))
        # Raised by stand-still recovery after its exclusive off-layer/far-X
        # phase ends.  A skipped beat must not impose another whole attack
        # interval before the first near-anchor attack can fire.
        self.attack_resume_event = attack_resume_event
        # A one-use key replacement shared with stationary recovery.  It does
        # not schedule an attack; it only changes the key for an approved
        # cadence beat, so a hit cannot race a movement correction.
        self.attack_slot = attack_slot
        # The sequence runner owns Alt -> airborne gap -> attack as one
        # combat-lane transaction.  This worker still owns the existing
        # MotionArbiter attack lease around that call; the sequence is not a
        # second queued arbiter motion and never touches patrol Left/Right.
        self.atomic_motion_runner = atomic_motion_runner
        # 站桩攻击 only.  In stand-still mode the anchor correction and 小碎步
        # carry their own attack as the beat itself, so the cadence must not add
        # a second one right after them (see ``spacing_wait_seconds``).  On
        # patrol the cadence is the primary attack source and 小碎步/被撞反击 are
        # deliberate extras, so their attacks must never push the cadence later
        # - the operator's patrol log showed the 2.50s cadence losing a beat to
        # a 小碎步 attack and the character then walking an abnormally long
        # stretch before its own attack.
        self.stationary_mode = False
        # The optional sequence planner is deliberately separate from this
        # worker: it only selects a key for an already-approved cadence beat.
        self.combo_attack = ComboAttackPlan()
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

    def set_combo_attack(self, enabled: bool, slots: object) -> None:
        """Apply a UI combo plan after rejecting unsupported non-empty keys."""

        scan_map = getattr(self.key_sender, "_SCAN", None)
        sanitized = []
        for raw_slot in slots if isinstance(slots, (list, tuple)) else ():
            slot = dict(raw_slot) if isinstance(raw_slot, dict) else {}
            raw_key = str(slot.get("key", "-")).strip().casefold()
            if raw_key not in ("", "-", "none", "null"):
                if scan_map is not None and raw_key not in scan_map:
                    LOG.warning("combo attack key %r unsupported; using idle beat", raw_key)
                    raw_key = "-"
            slot["key"] = raw_key
            sanitized.append(slot)
        self.combo_attack.configure(bool(enabled), sanitized)

    def attack_once(self, attack_key: Optional[str] = None) -> bool:
        """Send a game-recognizable short attack press; never move.

        A zero-duration down/up pair is visible in the debug log but can be
        ignored by MapleStory between input polls.  Prefer the key sender's
        normal ``tap`` method, which holds the scan code for 25 ms just like
        the reliable potion input path.

        In 跳跃攻击 mode the beat is a bundle: Alt for 45ms, then 155ms of
        airborne gap, then the configured attack key.  The attack therefore
        begins 200ms after Alt-down.
        """

        key = self.attack_key if attack_key is None else attack_key
        runner = self.atomic_motion_runner
        if runner is not None:
            return runner.execute_combat_attack(
                key,
                jump_attack_enabled=self.jump_attack,
                allowed=self._combat_input_allowed,
                label="fixed-cadence",
            )

        # Compatibility path for integrations that have not supplied the
        # atomic runner yet.
        if self.jump_attack:
            tap = getattr(self.key_sender, "tap", None)
            if not callable(tap):
                return False
            if tap("alt") is False:
                return False
            if self.stop_event.wait(self.jump_attack_delay):
                # Stopping mid-bundle: skip the attack half.
                return False
            attack_tap = getattr(self.key_sender, "tap_attack", None)
            if not callable(attack_tap):
                attack_tap = tap
            return attack_tap(key) is not False

        # A real WindowKeySender offers the paired two-line attack lifecycle
        # logger.  Small test/dry-run senders may only implement ``tap``;
        # retain that compatible fallback without changing their behaviour.
        tap = getattr(self.key_sender, "tap_attack", None)
        if not callable(tap):
            tap = getattr(self.key_sender, "tap", None)
        if callable(tap):
            return tap(key) is not False

        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is not None and key_up is not None:
            claimed = key_down(key) is not False
            if not claimed:
                return False
            return key_up(key) is not False
        return False

    def _combat_input_allowed(self) -> bool:
        """Whether a currently running combat bundle may complete safely."""

        if not self.enabled or self.stop_event.is_set():
            return False
        if (self.automation_active_event is not None
                and not self.automation_active_event.is_set()):
            return False
        return not (
            self.climbing_active_event is not None
            and self.climbing_active_event.is_set()
        )

    def next_delay(self) -> float:
        """Configured attack interval plus the configured random gap."""

        random_gap = random.uniform(0.0, self.attack_jitter_seconds)
        return max(0.05, self.attack_interval + random_gap)

    def spacing_wait_seconds(self) -> Optional[float]:
        """How long this beat must still wait to keep the attack spacing.

        站桩攻击 only (``stationary_mode``).  There the anchor correction walks
        back and taps the attack that belongs to its own motion, and 小碎步
        carries a middle attack, so that motion IS the beat: firing a cadence
        beat right after it is the second, quick attack the operator reported
        ("he went back to the 桩 and then attacked twice"), and the beat is
        rescheduled a full interval after that attack instead.

        On patrol those same motions are deliberate *extras* beside the fixed
        cadence, so this returns ``None`` and their attacks never push the
        cadence later.  ``None`` means the spacing is satisfied and the beat may
        fire.

        Measured from ``last_attack_at()`` (any owner) rather than from "did an
        attack appear while we waited": the correction taps before the arbiter
        sets its own busy window, so a beat that comes due just after that tap
        would otherwise see the attack as already accounted for and fire the
        moment the window expired.
        """

        if self.motion_arbiter is None or not self.stationary_mode:
            return None
        since_last_attack = time.monotonic() - self.motion_arbiter.last_attack_at()
        if since_last_attack >= self.attack_interval:
            return None
        return self.attack_interval - since_last_attack

    def run(self) -> None:
        LOG.info("attack worker started offset=%.3fs interval=%.3fs key=%s "
                 "enabled=%s",
                 self.initial_offset, self.attack_interval, self.attack_key,
                 self.enabled)
        next_attack = time.monotonic() + self.initial_offset
        while not self.stop_event.is_set():
            while not self.stop_event.is_set():
                resume = self.attack_resume_event
                if resume is not None and resume.is_set():
                    resume.clear()
                    next_attack = time.monotonic()
                    LOG.info("attack cadence resumed immediately after stand-still recovery")
                    break
                remaining = next_attack - time.monotonic()
                if remaining <= 0.0:
                    break
                # The dedicated event keeps this responsive without a second
                # worker or coupling normal attack cadence to movement frames.
                self.stop_event.wait(min(0.05, remaining))
            if self.stop_event.is_set():
                break
            # A beat must also keep a full interval after an attack another
            # owner made.  The 站桩 correction walks back and taps the attack
            # that belongs to its own motion, and 小碎步 carries a middle
            # attack; the operator's field report is the cadence landing its
            # catch-up beat a fraction of a second after one of those ("he went
            # back to the 桩 and then attacked twice").  A motion that only
            # moved (a buff, a jump) still keeps the ordinary catch-up beat,
            # which must not cost a whole interval after stand-still recovery.
            spacing_wait = self.spacing_wait_seconds()
            if spacing_wait is not None:
                LOG.info(
                    "attack beat rescheduled: the last attack was %.2fs ago "
                    "(a 站桩 correction or 小碎步 that carried its own attack); "
                    "the next beat keeps a full %.2fs away",
                    self.attack_interval - spacing_wait, self.attack_interval,
                )
                next_attack = time.monotonic() + spacing_wait
                continue
            can_fire = self.enabled
            attack_lease = False
            handoff_deferred = False
            selected_key: Optional[str] = None
            if can_fire and (self.automation_active_event is not None
                    and not self.automation_active_event.is_set()):
                can_fire = False
            if can_fire and (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                LOG.info("attack skipped: climb/return input is active")
                can_fire = False
            if (can_fire and self.direction_transition_event is not None
                    and self.direction_transition_event.is_set()):
                LOG.info("attack skipped: patrol direction handoff is active")
                can_fire = False
                handoff_deferred = True
            if can_fire:
                # Advance only for a beat that passed all non-arbiter gates.
                # A null combo run deliberately consumes this beat without
                # reserving motion input or emitting a key.
                normal_key = self.combo_attack.peek_key(self.attack_key)
                if normal_key is None:
                    self.combo_attack.consume()
                    LOG.info("combo attack idle beat")
                    can_fire = False
            reschedule_after_wait: Optional[float] = None
            if can_fire and self.motion_arbiter is not None:
                # Reservation is deliberately atomic.  Checking idle and
                # tapping separately allowed a queued 小碎步 to start between
                # those operations, so a directional sequence could overlap
                # the attack animation.  One lease owns this whole tap.
                attack_lease = self.motion_arbiter.try_begin_attack()
                if not attack_lease:
                    self.motion_arbiter.wait_until_idle()
                    # The motion we just waited for may have attacked as part
                    # of its own work (the 站桩 correction taps its attack at
                    # the end of its walk back).  That attack has now spent
                    # this beat, so it is rescheduled instead of firing a
                    # second attack a fraction of a second later.
                    reschedule_after_wait = self.spacing_wait_seconds()
                    attack_lease = bool(
                        reschedule_after_wait is None
                        and self.enabled
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
            if reschedule_after_wait is not None:
                LOG.info(
                    "attack beat rescheduled: the motion it waited for attacked "
                    "on its own (a 站桩 correction or 小碎步 carries its own "
                    "attack); the next beat keeps a full %.2fs away",
                    self.attack_interval,
                )
                next_attack = time.monotonic() + reschedule_after_wait
                continue
            if can_fire:
                # The one-use replacement belongs to the beat that really
                # attacks.  Claiming it before the lease let a deferred beat
                # hold the 被撞反击 key while the correction - which taps first,
                # as part of its own motion - used the ordinary key instead.
                slot = self.attack_slot
                consume = getattr(slot, "consume", None)
                selected_key = (
                    consume(normal_key) if callable(consume) else normal_key
                )
            if can_fire:
                sent = False
                try:
                    self.combo_attack.consume()
                    sent = self.attack_once(selected_key)
                finally:
                    if self.motion_arbiter is not None and attack_lease:
                        self.motion_arbiter.finish_attack(sent)
            if handoff_deferred:
                # A route endpoint turn deliberately reserves a short window
                # around Left-up -> Right-down (or the reverse).  Treating a
                # beat that lands in that window as a completed attack used to
                # lose a whole configured interval, creating the visible
                # ~2-second no-attack pause at every turn.  Wait quietly for
                # the handoff owner to clear its reservation, then retry now.
                while (not self.stop_event.is_set()
                       and self.direction_transition_event is not None
                       and self.direction_transition_event.is_set()):
                    self.stop_event.wait(0.02)
                next_attack = time.monotonic()
                continue
            # The random component is additive only: attack + random_gap.
            next_attack = time.monotonic() + self.next_delay()
        LOG.info("attack worker stopped")


__all__ = ["AttackWorker"]
