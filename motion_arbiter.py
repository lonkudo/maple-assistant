"""Serialize jump/buff taps against the fixed attack cadence.

MapleStory action motion drops a key pressed while the character is still
performing another action, so independent attack / random-jump / periodic
buff timers collide and the jump or buff tap silently does nothing.

``MotionArbiter`` is the single executor for those motion keys:

- a FIFO queue temporarily registers jump and buff events (requests are
  non-blocking; duplicate pending events collapse to one);
- one worker thread dequeues and executes them one at a time;
- a jump locks out further motion for ``jump_motion_seconds`` (default 0.9s)
  and a buff for ``buff_motion_seconds`` (default 0.6s);
- while events are queued or executing, fixed attack is suppressed;
- only when the motion window is over does the next event dequeue - or the
  attack worker fire again;
- every event additionally waits a short ``attack_grace_seconds`` (default
  0.73s) after the last attack tap, so an attack motion that just started
  cannot swallow the jump/buff tap.

Attack taps are not queued (they keep their own cadence), but they do acquire
an atomic short-lived reservation before emitting input.  This matters: an
``is_idle()`` check followed by a key tap is otherwise a race with a newly
queued micro-step.  HP/MP potions stay on their own urgent path (bar-verified
retries), outside this queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional


LOG = logging.getLogger(__name__)

JUMP = "jump"
MICRO_STEP = "micro_step"
STAIR_JUMP = "stair_jump"
FACING = "facing"
# 站桩攻击's anchor correction.  It is the "combination" motion: a tiny step (or
# a short walk back) that ends with the attack belonging to that correction.
# Like every other queued motion it excludes the fixed cadence while it is
# queued or running, and the attack it taps is what the character would
# otherwise lose while correcting its position.
STEP = "step"
COUNTERATTACK = "counterattack"

# A tap can be refused by the input layer (game window not foreground, input
# disarmed, focus stolen mid-tap).  A jump/micro-step is stale by then and is
# drained, but a periodic buff must NOT be lost: its timer will not ask again
# for minutes, so the token stays queued and is retried for this long.
_DELIVERY_RETRY_SECONDS = 6.0
_DELIVERY_RETRY_DELAY = 0.15
# A buff that waits for the safe-stage gate logs after this long, so "nothing
# happened" is never silent (the gate shuts whenever the character is not
# making a left/right walk decision).
_GATE_WAIT_WARN_SECONDS = 4.0


class MotionArbiter(threading.Thread):
    """One-at-a-time executor for jump/buff motion keys."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        *,
        climbing_active_event: Optional[threading.Event] = None,
        automation_active_event: Optional[threading.Event] = None,
        jump_motion_seconds: float = 0.9,
        buff_motion_seconds: float = 1.0,
        buff_tap_hold_seconds: float = 0.20,
        micro_step_motion_seconds: float = 0.25,
        micro_step_pre_step_quiet_seconds: float = 0.20,
        facing_motion_seconds: float = 0.10,
        # A stand-still correction ends with an attack tap, so its lock is the
        # same kind of hold a 小碎步 uses: the moment after the tap is where the
        # fixed cadence would otherwise re-tap on top of it.
        step_motion_seconds: float = 0.25,
        attack_grace_seconds: float = 0.73,
        delivery_retry_seconds: float = _DELIVERY_RETRY_SECONDS,
    ) -> None:
        super().__init__(name="motion-arbiter", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.climbing_active_event = climbing_active_event
        self.automation_active_event = automation_active_event
        self.jump_motion_seconds = max(0.0, float(jump_motion_seconds))
        self.buff_motion_seconds = max(0.0, float(buff_motion_seconds))
        self.buff_tap_hold_seconds = max(0.01, min(1.0, float(buff_tap_hold_seconds)))
        self.micro_step_motion_seconds = max(
            0.0, float(micro_step_motion_seconds)
        )
        # The shared attack grace ends the known attack animation window. A
        # 小碎步 needs a short neutral gap before its FIRST
        # direction: otherwise Maple may still consume that first hold while
        # accepting the later post-step direction normally.
        self.micro_step_pre_step_quiet_seconds = max(
            0.0, float(micro_step_pre_step_quiet_seconds)
        )
        self.facing_motion_seconds = max(0.0, float(facing_motion_seconds))
        # The 站桩 correction ends with an attack tap, so its lock keeps the
        # fixed cadence from re-tapping on top of that attack.
        self.step_motion_seconds = max(
            0.0, float(step_motion_seconds)
        )
        self.attack_grace_seconds = max(0.0, float(attack_grace_seconds))
        self.delivery_retry_seconds = max(0.0, float(delivery_retry_seconds))
        # How long a buff may wait for the safe-stage gate before it says so.
        self.gate_wait_warn_seconds = _GATE_WAIT_WARN_SECONDS
        # Installed after MovementWorker exists.  The arbiter serializes the
        # timing, while movement owns the directional key handoff itself.
        self._micro_step_callback: Any = None
        self._facing_callback: Any = None
        # The stand-still anchor correction is a finite motion too: a tiny step
        # that carries its own attack tap, so it is serialized here instead of
        # being sent as an ordinary walk beside the fixed cadence.
        self._step_callback: Any = None
        self._counterattack_callback: Any = None
        # A confirmed stair stall borrows patrol's current direction and taps
        # Alt.  It is queued only to serialize against attacks; it is not an
        # ordinary directional arbiter motion.
        self._stair_jump_callback: Any = None
        # Movement owns direction holds, so a queued buff borrows that same
        # owner for one atomic key tap before patrol resumes.
        self._buff_callback: Any = None
        self._motion_gate_callback: Any = None
        self._cv = threading.Condition()
        self._pending: "deque[str]" = deque()
        # Dedupe identities: "jump" or "buff:<key>".  A pending event of the
        # same kind means "do it once when free" - never pile up repeats.
        self._queued: set[str] = set()
        self._busy_until = 0.0  # monotonic end of the running motion window
        self._last_attack_at = float("-inf")
        # Attack reservations make the idle-check/tap pair atomic.  A queued
        # motion sees this reservation and waits; a fresh attack sees a queued
        # motion and defers.  Therefore neither can begin inside the other.
        self._attack_reserved = False
        self._executing_token: Optional[str] = None
        # A confirmed stair recovery owns Alt until MovementWorker has seen
        # genuine horizontal travel again.  This prevents the independent
        # random-jump timer from adding visually identical extra jumps while
        # the stair recovery is still settling.
        self._stair_recovery_locked = False
        # Completion callbacks belong to periodic-buff timers.  They are
        # invoked only once the key has completed its full motion window.
        self._buff_completion_callbacks: dict[str, list[Any]] = {}
        # Stair detection must know whether its one queued action actually
        # pressed Alt.  A failed/aborted queue entry is not a completed stair
        # recovery and must be eligible for a later retry.
        self._stair_jump_completion_callbacks: dict[str, list[Any]] = {}
        # Delivery retry bookkeeping for taps that were refused by the input
        # layer (see _DELIVERY_RETRY_SECONDS).
        self._delivery_deadline: dict[str, float] = {}
        # Why the last request/execution was refused, for the callers' logs.
        self._last_refusal = ""

    # ------------------------------------------------------------------ #
    # Registration (any thread)                                          #
    # ------------------------------------------------------------------ #

    def request_jump(self) -> bool:
        """Queue one jump (Alt tap). Duplicate pending jumps collapse."""

        with self._cv:
            if not self._automation_allowed_locked():
                self._set_refusal_locked("automation inactive (stop or patrol off)")
                return False
            if not self._motion_gate_allows_locked():
                self._set_refusal_locked("movement is not in a walkable stage")
                return False
            if self._stair_recovery_locked:
                self._set_refusal_locked("stair recovery owns Alt")
                return False
            if JUMP in self._queued:
                return True
            self._pending.append(JUMP)
            self._queued.add(JUMP)
            self._cv.notify_all()
            return True

    def set_stair_recovery_lock(self, locked: bool) -> None:
        """Reserve Alt for a detected stair until travel is confirmed again."""

        with self._cv:
            self._stair_recovery_locked = bool(locked)
            if self._stair_recovery_locked and JUMP in self._queued:
                # A generic random-jump token waiting behind an attack is not
                # part of the stair recovery.  Drain it before it can create
                # a second Alt press after the direction-preserving jump.
                self._pending = deque(
                    token for token in self._pending if token != JUMP
                )
                self._queued.discard(JUMP)
            self._cv.notify_all()

    def request_buff(self, key: str, on_complete: Any = None) -> bool:
        """Queue one periodic buff key tap and notify after it completes.

        Buffs may be registered while climbing or transitioning, but are held
        until MovementWorker reports a safe left/right or rope-approach stage.
        This prevents an elapsed buff timer from being silently lost.
        """

        token = f"buff:{str(key).casefold()}"
        with self._cv:
            if not self._automation_allowed_locked():
                return False
            if token in self._queued:
                if callable(on_complete):
                    self._buff_completion_callbacks.setdefault(token, []).append(
                        on_complete
                    )
                return True
            self._pending.append(token)
            self._queued.add(token)
            if callable(on_complete):
                self._buff_completion_callbacks[token] = [on_complete]
            self._cv.notify_all()
            return True

    def request_micro_step(self) -> bool:
        """Queue one Left/Right micro-step; duplicate requests collapse."""

        with self._cv:
            if not self._automation_allowed_locked():
                self._set_refusal_locked("automation inactive (stop or patrol off)")
                return False
            if not self._motion_gate_allows_locked():
                self._set_refusal_locked("movement is not in a walkable stage")
                return False
            if MICRO_STEP in self._queued:
                return True
            self._pending.append(MICRO_STEP)
            self._queued.add(MICRO_STEP)
            self._cv.notify_all()
            return True

    def request_facing(self, direction: str) -> bool:
        """Queue one short Left/Right facing correction after a recovery."""

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            return False
        token = f"{FACING}:{direction}"
        with self._cv:
            if not self._automation_allowed_locked():
                self._set_refusal_locked("automation inactive (stop or patrol off)")
                return False
            if not self._motion_gate_allows_locked():
                self._set_refusal_locked("movement is not in a safe facing stage")
                return False
            if token in self._queued:
                return True
            self._pending.append(token)
            self._queued.add(token)
            self._cv.notify_all()
            return True

    def facing_pending(self, direction: str) -> bool:
        """Whether a facing correction for *direction* is queued or running.

        Callers own the correction as an OBLIGATION: a token can be drained
        without ever reaching its callback (the safe-stage gate shuts for a walk
        handoff, or a focus dip disarms input), and a drained token must not be
        mistaken for an applied facing.  Asking the queue itself lets the owner
        re-request the correction on the next settled frame.
        """

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            return False
        token = f"{FACING}:{direction}"
        with self._cv:
            return token in self._queued or token == self._executing_token

    def request_step(self, direction: str) -> bool:
        """Queue one 站桩 anchor correction (the step-plus-attack combination).

        The motion itself is a short direction hold or a short walk back,
        followed by the attack that belongs to that correction - so it is
        queued rather than walked: the movement loop never blocks on a key
        hold, no jump/buff tap lands inside it, and the correction cannot
        become the frame that loses a fixed-attack beat.
        """

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            return False
        token = f"{STEP}:{direction}"
        with self._cv:
            if not self._automation_allowed_locked():
                self._set_refusal_locked("automation inactive (stop or patrol off)")
                return False
            if not self._motion_gate_allows_locked():
                self._set_refusal_locked("movement is not in a safe step stage")
                return False
            if token in self._queued:
                return True
            self._pending.append(token)
            self._queued.add(token)
            self._cv.notify_all()
            return True

    def request_counterattack(self, direction: str, key: str) -> bool:
        """Queue one HP-hit counter: step away, then tap its dedicated key."""

        direction = str(direction).casefold()
        key = str(key).casefold()
        if direction not in ("left", "right") or not key or key == "-":
            return False
        token = f"{COUNTERATTACK}:{direction}:{key}"
        with self._cv:
            if not self._automation_allowed_locked():
                self._set_refusal_locked("automation inactive (stop or patrol off)")
                return False
            # HP may fall across several frames for one hit.  One queued
            # retaliation is enough; never stack delayed walks.
            if any(item.startswith(f"{COUNTERATTACK}:") for item in self._queued):
                return True
            self._pending.append(token)
            self._queued.add(token)
            self._cv.notify_all()
            return True

    def step_pending(self) -> bool:
        """Whether an anchor correction is queued or running.

        The correction owner asks before queueing another one: each correction
        moves the marker by about a pixel, so two of them in flight would walk
        in opposite directions and net out, and each carries its own attack.
        """

        with self._cv:
            return any(
                token.startswith(f"{STEP}:") for token in self._queued
            )

    def micro_step_pending(self) -> bool:
        """Whether the optional 小碎步 pair is queued or running.

        The movement loop asks before issuing a position correction or a 朝向
        tap.  The pair moves the character deliberately (away from, then back
        to the selected 朝向) and sets the facing itself when it ends, so a
        correction that answers the pair's own step is the extra step (and extra
        attack) the operator saw right after 小碎步 and does not want.
        """

        with self._cv:
            return MICRO_STEP in self._queued

    @staticmethod
    def _is_step_token(token: Optional[str]) -> bool:
        """Whether *token* is a 站桩 anchor correction."""

        return bool(token) and str(token).startswith(f"{STEP}:")

    def request_stair_jump(self, direction: str, on_complete: Any = None) -> bool:
        """Queue a confirmed direction-preserving stair jump.

        Unlike ``request_jump``, this action does not replace patrol walking:
        its movement callback holds the current left/right direction and
        taps Alt.  The queue merely gives it exclusive time after an attack.
        """

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            return False
        token = f"{STAIR_JUMP}:{direction}"
        with self._cv:
            if not self._automation_allowed_locked():
                return False
            if token in self._queued:
                if callable(on_complete):
                    self._stair_jump_completion_callbacks.setdefault(
                        token, []
                    ).append(on_complete)
                return True
            self._pending.append(token)
            self._queued.add(token)
            if callable(on_complete):
                self._stair_jump_completion_callbacks[token] = [on_complete]
            self._cv.notify_all()
            return True

    def set_micro_step_callback(self, callback: Any) -> None:
        """Install MovementWorker's serialized micro-step implementation."""

        with self._cv:
            self._micro_step_callback = callback

    def set_facing_callback(self, callback: Any) -> None:
        """Install MovementWorker's atomic one-direction facing action."""

        with self._cv:
            self._facing_callback = callback

    def set_step_callback(self, callback: Any) -> None:
        """Install MovementWorker's atomic 站桩 correction (step + its attack)."""

        with self._cv:
            self._step_callback = callback

    def set_counterattack_callback(self, callback: Any) -> None:
        """Install MovementWorker's isolated 被撞反击 implementation."""

        with self._cv:
            self._counterattack_callback = callback

    def set_stair_jump_callback(self, callback: Any) -> None:
        """Install MovementWorker's direction-preserving stair-jump action."""

        with self._cv:
            self._stair_jump_callback = callback

    def stair_jump_pending(self) -> bool:
        """Whether a confirmed stair jump owns the attack exclusion window."""

        with self._cv:
            return any(token.startswith(f"{STAIR_JUMP}:") for token in self._queued)

    def set_buff_callback(self, callback: Any) -> None:
        """Install MovementWorker's atomic buff handoff implementation."""

        with self._cv:
            self._buff_callback = callback

    def set_motion_gate_callback(self, callback: Any) -> None:
        """Allow queued motion only when MovementWorker reports safe travel."""

        with self._cv:
            self._motion_gate_callback = callback

    def cancel_pending(self, reason: str = "patrol stopped") -> None:
        """Drain queued automation motions at a patrol lifecycle boundary.

        Stop Patrol is not merely a UI state change: a jump, buff, micro-step,
        or facing correction may already be waiting behind an attack grace
        window.  Leaving that token queued lets it run after the user believes
        automation has stopped (or after the next start), so cancel the queue
        synchronously before the input sender performs its neutral key scrub.
        """

        with self._cv:
            tokens = list(self._pending)
            self._pending.clear()
            self._queued.clear()
            self._delivery_deadline.clear()
            # An attack reservation is only a logical lease.  Its sender will
            # observe the disarmed automation gate, while clearing the lease
            # here prevents a stopped session from blocking the next one.
            self._attack_reserved = False
            callbacks: list[Any] = []
            for token in tokens:
                callbacks.extend(self._buff_completion_callbacks.pop(token, []))
                callbacks.extend(
                    self._stair_jump_completion_callbacks.pop(token, [])
                )
            self._cv.notify_all()
        self._notify_buff_completion(callbacks, False)
        if tokens:
            LOG.info("motion arbiter cancelled %d pending action(s): %s",
                     len(tokens), reason)

    def _set_refusal_locked(self, reason: str) -> None:
        """Record why the arbiter refused (so caller logs can name it)."""

        self._last_refusal = reason

    def last_refusal(self) -> str:
        """Why the most recent request/execution was refused ('' when none)."""

        with self._cv:
            return self._last_refusal

    def _automation_allowed_locked(self) -> bool:
        return bool(
            not self.stop_event.is_set()
            and (self.automation_active_event is None
                 or self.automation_active_event.is_set())
        )

    def _motion_gate_allows_locked(self) -> bool:
        callback = self._motion_gate_callback
        if not callable(callback):
            return True
        try:
            return bool(callback())
        except Exception:
            LOG.exception("motion arbiter safe-stage gate failed")
            return False

    def try_begin_attack(self) -> bool:
        """Atomically reserve the keyboard for one fixed-attack tap.

        Call ``finish_attack`` in a ``finally`` block after a successful
        reservation.  This replaces the unsafe ``is_idle`` then ``tap``
        sequence: a micro-step, jump, buff, or anchor correction cannot slip
        between those two operations and change the character's direction
        mid-animation.
        """

        with self._cv:
            pending_blocks = bool(self._pending) and (
                any(not token.startswith("buff:") for token in self._pending)
                or self._motion_gate_allows_locked()
            )
            if (not self._automation_allowed_locked()
                    or self._attack_reserved
                    or self._executing_token is not None
                    or pending_blocks
                    or time.monotonic() < self._busy_until):
                return False
            self._attack_reserved = True
            return True

    def finish_attack(self, sent: bool) -> None:
        """Release an attack reservation and start its grace window if sent."""

        with self._cv:
            if sent:
                self._last_attack_at = time.monotonic()
            self._attack_reserved = False
            self._cv.notify_all()

    def note_attack(self) -> None:
        """Backward-compatible shorthand for legacy direct attack callers."""

        with self._cv:
            self._last_attack_at = time.monotonic()
            self._cv.notify_all()

    def attack_motion_active(self) -> bool:
        """True while a fixed-attack animation can mask movement progress."""

        with self._cv:
            return bool(
                self._attack_reserved
                or time.monotonic() - self._last_attack_at
                < self.attack_grace_seconds
            )

    # ------------------------------------------------------------------ #
    # Attack-facing state                                                #
    # ------------------------------------------------------------------ #

    def is_idle(self) -> bool:
        """True when no event is queued and no motion window is running."""

        with self._cv:
            return (not self._pending
                    and not self._attack_reserved
                    and self._executing_token is None
                    and time.monotonic() >= self._busy_until)

    def wait_until_idle(self) -> bool:
        """Block until the arbiter is idle; False when stopped first."""

        with self._cv:
            while not self.stop_event.is_set():
                if (not self._pending
                        and not self._attack_reserved
                        and self._executing_token is None
                        and time.monotonic() >= self._busy_until):
                    return True
                self._cv.wait(0.1)
            return False

    # ------------------------------------------------------------------ #
    # Executor thread                                                    #
    # ------------------------------------------------------------------ #

    def _duration_for(self, token: str) -> float:
        if token == JUMP:
            return self.jump_motion_seconds
        if token == MICRO_STEP:
            return self.micro_step_motion_seconds
        if token.startswith(f"{FACING}:"):
            return self.facing_motion_seconds
        if token.startswith(f"{STEP}:"):
            return self.step_motion_seconds
        if token.startswith(f"{COUNTERATTACK}:"):
            return self.step_motion_seconds
        return self.buff_motion_seconds

    def _pop_locked(self, token: str) -> list[Any]:
        """Drop *token* from the queue head (single executor owns the head)."""

        if self._pending and self._pending[0] == token:
            self._pending.popleft()
        self._queued.discard(token)
        callbacks = self._buff_completion_callbacks.pop(token, [])
        callbacks.extend(self._stair_jump_completion_callbacks.pop(token, []))
        return callbacks

    @staticmethod
    def _notify_buff_completion(callbacks: list[Any], succeeded: bool) -> None:
        for callback in callbacks:
            try:
                callback(succeeded)
            except Exception:
                LOG.exception("motion arbiter buff completion callback failed")

    def run(self) -> None:
        LOG.info("motion arbiter started")
        while not self.stop_event.is_set():
            with self._cv:
                while not self._pending and not self.stop_event.is_set():
                    self._cv.wait(0.2)
                if self.stop_event.is_set():
                    break
                token = self._pending[0]
            self._execute(token)
        LOG.info("motion arbiter stopped")

    def _execute(self, token: str) -> None:
        # A request made before Stop Patrol must never execute later.  Drop it
        # here as well as at registration because it may have been queued just
        # before the automation gate was cleared.
        is_stair_jump = token.startswith(f"{STAIR_JUMP}:")
        is_counterattack = token.startswith(f"{COUNTERATTACK}:")
        waiting_since = time.monotonic()
        wait_warned_at = 0.0
        with self._cv:
            while not self.stop_event.is_set():
                if not self._automation_allowed_locked():
                    callbacks = self._pop_locked(token)
                    self._cv.notify_all()
                    self._notify_buff_completion(callbacks, False)
                    return
                if (not is_stair_jump and not is_counterattack
                        and not self._motion_gate_allows_locked()):
                    if token.startswith("buff:"):
                        # A buff stays registered through climb/drop/landing
                        # and wakes itself as soon as ordinary travel returns.
                        # It must never wait silently: this is the one state
                        # where the log shows nothing at all while a buff that
                        # was due minutes ago never fires.
                        now = time.monotonic()
                        if (now - waiting_since >= self.gate_wait_warn_seconds
                                and now - wait_warned_at
                                >= self.gate_wait_warn_seconds):
                            wait_warned_at = now
                            LOG.warning(
                                "motion arbiter waiting %.1fs to emit %s: "
                                "movement reports no safe walking stage "
                                "(the gate stays shut while the character is "
                                "not making a left/right walk decision)",
                                now - waiting_since,
                                token,
                            )
                        self._cv.wait(0.10)
                        continue
                    if (token.startswith(f"{FACING}:")
                            or token.startswith(f"{STEP}:")):
                        # Movement owns the facing and the 桩 step as obligations
                        # and re-asks on the next settled frame, so a drained
                        # token is safe - but it must be visible: without this
                        # line the log shows a correction that was queued and
                        # then simply never happened.
                        LOG.info(
                            "motion arbiter dropped %s: the safe-stage gate "
                            "shut before its turn; movement will re-request",
                            token,
                        )
                    callbacks = self._pop_locked(token)
                    self._cv.notify_all()
                    self._notify_buff_completion(callbacks, False)
                    return
                if self._attack_reserved:
                    self._cv.wait(0.05)
                    continue
                self._executing_token = token
                break
            else:
                return
        # Wait out the tail of any attack motion before pressing a motion
        # key; the token stays queued meanwhile, so attack stays suppressed.
        # The 站桩 correction is exempt because it carries its own attack and
        # registers it (``note_attack``): waiting the shared grace after its own
        # tap would stretch a correction that the operator wants about 0.3s
        # apart out to one per second.
        if not self._is_step_token(token):
            while not self.stop_event.is_set():
                with self._cv:
                    grace = self.attack_grace_seconds - (
                        time.monotonic() - self._last_attack_at
                    )
                if grace <= 0:
                    break
                self.stop_event.wait(grace)
        if self.stop_event.is_set():
            with self._cv:
                self._executing_token = None
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
            self._notify_buff_completion(callbacks, False)
            return

        if token == MICRO_STEP and self.micro_step_pre_step_quiet_seconds:
            LOG.info(
                "motion arbiter micro-step: waiting %.2fs after attack grace "
                "before first direction",
                self.micro_step_pre_step_quiet_seconds,
            )
            if self.stop_event.wait(self.micro_step_pre_step_quiet_seconds):
                with self._cv:
                    self._executing_token = None
                    callbacks = self._pop_locked(token)
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                return

        # The grace wait can overlap a movement transition.  Recheck the safe
        # stage immediately before injecting the key; a buff remains queued
        # and tries again later, whereas jump/micro-step are stale and drain.
        with self._cv:
            dropped = False
            if not self._automation_allowed_locked():
                self._executing_token = None
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
                dropped = True
            elif (not is_stair_jump and not is_counterattack
                  and not self._motion_gate_allows_locked()):
                self._executing_token = None
                if token.startswith("buff:"):
                    self._cv.notify_all()
                    return
                if (token.startswith(f"{FACING}:")
                        or token.startswith(f"{STEP}:")):
                    LOG.info(
                        "motion arbiter dropped %s: the safe-stage gate shut "
                        "during the attack grace; movement will re-request",
                        token,
                    )
                callbacks = self._pop_locked(token)
                self._cv.notify_all()
                dropped = True
            else:
                callbacks = []
        if dropped:
            self._notify_buff_completion(callbacks, False)
            return

        if is_stair_jump:
            with self._cv:
                callback = self._stair_jump_callback
            try:
                tap_ok = callable(callback) and callback(
                    token.partition(":")[2]
                ) is not False
            except Exception:
                LOG.exception("motion arbiter stair jump failed")
                tap_ok = False
            with self._cv:
                callbacks = self._pop_locked(token)
                self._executing_token = None
                self._cv.notify_all()
            self._notify_buff_completion(callbacks, tap_ok)
            if tap_ok:
                LOG.info("motion arbiter executed direction-preserving %s", token)
            else:
                LOG.warning("motion arbiter stair jump blocked; event drained")
            return

        if token == JUMP:
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                # Movement owns Alt during climb/return: drop the stale jump
                # instead of injecting a chord-breaking Alt press.
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.info("motion arbiter dropped jump: climb/return input "
                         "is active")
                return
            key = "alt"
        elif token == MICRO_STEP:
            if (self.climbing_active_event is not None
                    and self.climbing_active_event.is_set()):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.info("motion arbiter dropped %s: climb/return input is active", token)
                return
            with self._cv:
                callback = self._micro_step_callback
            if not callable(callback):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.warning("motion arbiter dropped %s: movement unavailable", token)
                return
            try:
                tap_ok = callback() is not False
            except Exception:
                LOG.exception("motion arbiter %s failed", token)
                tap_ok = False
            with self._cv:
                self._pop_locked(token)
                self._executing_token = None
                if tap_ok:
                    self._busy_until = time.monotonic() + self._duration_for(token)
                self._cv.notify_all()
            if tap_ok:
                LOG.info("motion arbiter executed %s (lock %.2fs)", token,
                         self._duration_for(token))
                self.stop_event.wait(self._duration_for(token))
            else:
                LOG.warning("motion arbiter %s NOT delivered; event drained", token)
            return
        elif token.startswith(f"{COUNTERATTACK}:"):
            _, direction, key = token.split(":", 2)
            with self._cv:
                callback = self._counterattack_callback
            if not callable(callback):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.warning("motion arbiter dropped counterattack: movement unavailable")
                return
            try:
                tap_ok = callback(direction, key) is not False
            except Exception:
                LOG.exception("motion arbiter counterattack failed")
                tap_ok = False
            with self._cv:
                self._pop_locked(token)
                self._executing_token = None
                if tap_ok:
                    self._busy_until = time.monotonic() + self._duration_for(token)
                self._cv.notify_all()
            if tap_ok:
                LOG.info("motion arbiter executed %s", token)
                self.stop_event.wait(self._duration_for(token))
            else:
                LOG.info("motion arbiter counterattack not delivered; event drained")
            return
        elif (token.startswith(f"{FACING}:")
                or token.startswith(f"{STEP}:")):
            # Both are MovementWorker's atomic one-direction actions and both
            # take the direction as their argument, so they share one branch:
            # the facing tap turns the character after a recovery, the 站桩
            # correction holds a direction and then taps the attack that
            # belongs to it (the step-plus-attack combination).
            is_step = token.startswith(f"{STEP}:")
            with self._cv:
                callback = (self._step_callback if is_step
                            else self._facing_callback)
            label = "anchor correction" if is_step else "facing correction"
            if not callable(callback):
                with self._cv:
                    callbacks = self._pop_locked(token)
                    self._executing_token = None
                    self._cv.notify_all()
                self._notify_buff_completion(callbacks, False)
                LOG.warning(
                    "motion arbiter dropped %s: movement unavailable", label
                )
                return
            direction = token.partition(":")[2]
            try:
                tap_ok = callback(direction) is not False
            except Exception:
                LOG.exception("motion arbiter %s failed", label)
                tap_ok = False
            with self._cv:
                self._pop_locked(token)
                self._executing_token = None
                if tap_ok:
                    self._busy_until = time.monotonic() + self._duration_for(token)
                self._cv.notify_all()
            if tap_ok:
                LOG.info("motion arbiter executed %s", token)
                self.stop_event.wait(self._duration_for(token))
            else:
                LOG.warning(
                    "motion arbiter %s NOT delivered; event drained", label
                )
            return
        else:
            key = token.partition(":")[2]

        tap_ok = False
        try:
            callback = None
            if token.startswith("buff:"):
                with self._cv:
                    callback = self._buff_callback
            if callable(callback):
                tap_ok = callback(key, self.buff_tap_hold_seconds) is not False
            else:
                tap_ok = self.key_sender.tap(key, hold_seconds=self.buff_tap_hold_seconds) is not False
        except Exception:
            LOG.exception("motion arbiter tap failed key=%s", key)
        if tap_ok:
            with self._cv:
                callbacks = self._pop_locked(token)
                self._delivery_deadline.pop(token, None)
                self._executing_token = None
                self._busy_until = time.monotonic() + self._duration_for(token)
                self._cv.notify_all()
            duration = self._duration_for(token)
            LOG.info("motion arbiter executed %s (lock %.2fs)", token, duration)
            if duration > 0:
                # Only the next dequeue - or an attack - may run after this.
                self.stop_event.wait(duration)
            self._notify_buff_completion(
                callbacks, not self.stop_event.is_set()
            )
            return
        # The input layer refused the tap (game window not foreground, input
        # disarmed, or the focus was stolen mid-tap).  Measured in the field:
        # a tap can be "sent" and still do nothing, so it must be retried - and
        # a periodic buff in particular must not be lost, because its timer
        # will not ask again for minutes.
        now = time.monotonic()
        deadline = self._delivery_deadline.setdefault(
            token, now + self.delivery_retry_seconds
        )
        keep_queued = (
            token.startswith("buff:")
            and now < deadline
            and not self.stop_event.is_set()
        )
        with self._cv:
            self._executing_token = None
            if keep_queued:
                self._cv.notify_all()
                callbacks = []
            else:
                callbacks = self._pop_locked(token)
                self._delivery_deadline.pop(token, None)
                self._cv.notify_all()
        if keep_queued:
            LOG.warning(
                "motion arbiter could NOT deliver %s; kept queued and retried "
                "(%.1fs left)",
                token, max(0.0, deadline - now),
            )
            self.stop_event.wait(_DELIVERY_RETRY_DELAY)
            return
        LOG.warning(
            "motion arbiter NOT delivered %s (key=%s); event drained",
            token, key,
        )
        self._notify_buff_completion(callbacks, False)


__all__ = ["MotionArbiter"]
