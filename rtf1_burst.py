"""Bounded RTF1 frame pipeline for one RoiTrack WebSocket session.

This module deliberately owns *protocol flow control* only.  It does not know
about game capture, the mouse, overlays, or UI.  Its caller remains the sole
owner of the WebSocket, so reads and writes can never race across threads.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional


# RoiTrack 2.7.0 §3: maximum accepted frames that have been sent but have not
# yet received a frame_result.  Keep this local rather than leaking the limit
# into capture/UI workers.
IN_FLIGHT_LIMITS = {3: 2, 4: 3, 5: 3, 6: 4, 7: 4}


@dataclass(frozen=True)
class BurstResult:
    """A server answer paired with the context saved when its frame was sent."""

    frame_id: int
    answer: dict
    context: Any


class Rtf1BurstSession:
    """One ordered, bounded RTF1 pipeline over an already-handshaken client.

    ``submit`` never sends more than the negotiated frame-standard window.
    ``poll`` is called by the same worker and returns every available result in
    frame order.  There is intentionally no writer thread and no socket lock.
    """

    def __init__(self, frame_standard: float) -> None:
        standard = max(3, min(7, int(round(float(frame_standard)))))
        self.frame_standard = standard
        self.max_in_flight = IN_FLIGHT_LIMITS[standard]
        self._pending: "OrderedDict[int, Any]" = OrderedDict()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def has_capacity(self) -> bool:
        return self.pending_count < self.max_in_flight

    def submit(self, client: Any, *, frame_id: int, jpeg: bytes,
               frame_interval_sec: float, context: Any = None) -> bool:
        """Send exactly one RTF1 binary frame if the server window has room.

        Returns ``False`` without touching the socket when the in-flight window
        is full.  The caller may retain or discard that newly captured frame;
        for live tracking, discarding stale frames is normally preferable.
        """

        frame_id = int(frame_id)
        if not self.has_capacity:
            return False
        if frame_id in self._pending:
            raise ValueError(f"frame_id {frame_id} is already in flight")
        client.send_frame_rtf1(frame_id, jpeg, frame_interval_sec=frame_interval_sec)
        self._pending[frame_id] = context
        return True

    def poll(self, client: Any, *, timeout: float = 0.0) -> list[BurstResult]:
        """Drain readable JSON ``frame_result`` messages and free their slots."""

        messages = client.poll_results(timeout=max(0.0, float(timeout)))
        ready: list[BurstResult] = []
        for answer in messages:
            try:
                frame_id = int(answer.get("frame_id"))
            except (TypeError, ValueError):
                continue
            if frame_id not in self._pending:
                # A reconnect or a stale server message must not affect the
                # current window.  The caller can still inspect its run log.
                continue
            context = self._pending.pop(frame_id)
            ready.append(BurstResult(frame_id, answer, context))
        return ready

    def discard(self) -> None:
        """Forget pending frame contexts when the round/socket is abandoned."""

        self._pending.clear()


__all__ = ["BurstResult", "IN_FLIGHT_LIMITS", "Rtf1BurstSession"]
