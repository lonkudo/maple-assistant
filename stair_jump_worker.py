"""Run confirmed stair jumps without pausing the patrol walk.

Stair detection belongs to :mod:`movement_worker` because it consumes the
minimap position stream.  The resulting Alt tap, however, must be independent
of that loop: it waits for an in-flight fixed attack to finish while normal
Left/Right patrol input continues.  This worker owns exactly one confirmed
stair action at a time, so a frozen marker can never pile up multiple jumps.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional


LOG = logging.getLogger(__name__)


class StairJumpWorker(threading.Thread):
    """Dedicated one-at-a-time executor for direction-preserving stair jumps."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        automation_active_event: Optional[threading.Event] = None,
        action_active_event: Optional[threading.Event] = None,
        motion_arbiter: Any = None,
        execute_callback: Any = None,
    ) -> None:
        super().__init__(name="stair-jump-worker", daemon=True)
        self.stop_event = stop_event
        self.automation_active_event = automation_active_event
        self.action_active_event = action_active_event
        self.motion_arbiter = motion_arbiter
        self.execute_callback = execute_callback
        self._cv = threading.Condition()
        self._requested_direction: Optional[str] = None
        self._active = False
        self._completion_callbacks: list[Any] = []

    def _automation_allowed_locked(self) -> bool:
        return bool(
            not self.stop_event.is_set()
            and (
                self.automation_active_event is None
                or self.automation_active_event.is_set()
            )
        )

    def request(self, direction: str, on_complete: Any = None) -> bool:
        """Register one jump and immediately reserve its attack-free window.

        A request while one is already waiting or executing intentionally
        collapses into that same action.  It must not create a second Alt tap.
        """

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            return False
        with self._cv:
            if not self._automation_allowed_locked():
                return False
            if self._active:
                return True
            self._requested_direction = direction
            self._active = True
            if callable(on_complete):
                self._completion_callbacks = [on_complete]
            else:
                self._completion_callbacks = []
            if self.action_active_event is not None:
                # This is set before the wait so no *new* fixed attack can
                # begin between detection and the eventual Alt tap.
                self.action_active_event.set()
            self._cv.notify_all()
            return True

    def is_active(self) -> bool:
        """Return whether a confirmed jump is waiting or executing."""

        with self._cv:
            return self._active

    def _attack_motion_active(self) -> bool:
        probe = getattr(self.motion_arbiter, "attack_motion_active", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            LOG.exception("stair jump could not read attack state")
            return False

    def _finish(self, succeeded: bool) -> None:
        with self._cv:
            callbacks = self._completion_callbacks
            self._completion_callbacks = []
            self._requested_direction = None
            self._active = False
            if self.action_active_event is not None:
                # This is the stair worker's own exclusion event, not the
                # shared climb/return state. Clear it immediately so an empty
                # route can continue fixed attack and 小碎步 after the one tap.
                self.action_active_event.clear()
            self._cv.notify_all()
        for callback in callbacks:
            try:
                callback(bool(succeeded))
            except Exception:
                LOG.exception("stair jump completion callback failed")

    def run(self) -> None:
        LOG.info("stair jump worker started")
        while not self.stop_event.is_set():
            with self._cv:
                while (
                    self._requested_direction is None
                    and not self.stop_event.is_set()
                ):
                    self._cv.wait(0.20)
                if self.stop_event.is_set():
                    break
                direction = self._requested_direction

            # The old queue released the patrol direction while it waited
            # here.  This independent worker only waits for the tail of the
            # current attack; MovementWorker keeps its normal walk hold alive.
            while (
                direction is not None
                and not self.stop_event.is_set()
                and self._attack_motion_active()
            ):
                self.stop_event.wait(0.02)

            succeeded = False
            if direction is not None:
                with self._cv:
                    allowed = self._automation_allowed_locked()
                if allowed:
                    try:
                        succeeded = bool(self.execute_callback(direction))
                    except Exception:
                        LOG.exception("stair jump action failed")
            self._finish(succeeded)
            if succeeded:
                LOG.info("stair jump worker executed direction-preserving jump")
            elif not self.stop_event.is_set():
                LOG.warning("stair jump worker dropped pending jump")

        # An application shutdown must not leave the shared attack gate set.
        self._finish(False)
        LOG.info("stair jump worker stopped")


__all__ = ["StairJumpWorker"]
