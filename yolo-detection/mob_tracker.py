#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Temporal mob confirmation to reject flash false positives.

A single-frame YOLO detection may be a false positive ("a monster detected
in a flash").  ``MobTracker`` requires a detection to persist for
``confirm_frames`` consecutive frames before it is reported as a real mob,
and keeps confirmed mobs alive for up to ``miss_hold`` missing frames so
the target box does not flicker on a one-frame miss.

Detections are matched to tracks by center distance (nearest within
``match_px``).  The tracker is stateless between calls except for its
internal track table, so it can be shared across frames from any caller.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np


class MobTracker:
    """Confirm mob detections across frames; reject single-frame flashes."""

    def __init__(
        self,
        confirm_frames: int = 3,
        miss_hold: int = 3,
        match_px: float = 100.0,
    ) -> None:
        self.confirm_frames = max(1, int(confirm_frames))
        self.miss_hold = max(0, int(miss_hold))
        self.match_px = max(10.0, float(match_px))
        # track_id -> {"det": Detection, "seen": int, "misses": int}
        self._tracks: Dict[int, dict] = {}
        self._next_id = 0

    def update(self, detections: List[object]) -> List[object]:
        """Feed one frame of raw detections; return the confirmed mobs.

        Matching is greedy nearest-neighbor: each raw detection is assigned
        to the closest existing track within ``match_px``.  New detections
        start a track with ``seen=1``; a track is reported only once
        ``seen >= confirm_frames``.  Tracks with more than ``miss_hold``
        consecutive missing frames are dropped.
        """

        if not detections:
            return self._advance_misses(set())

        matched: set = set()
        for det in detections:
            best_id: Optional[int] = None
            best_dist = self.match_px
            for track_id, track in self._tracks.items():
                if track_id in matched:
                    continue
                dist = float(np.hypot(
                    det.center[0] - track["det"].center[0],
                    det.center[1] - track["det"].center[1],
                ))
                if dist < best_dist:
                    best_dist = dist
                    best_id = track_id
            if best_id is not None:
                matched.add(best_id)
                track = self._tracks[best_id]
                track["det"] = det
                # Consecutive-sighting counter: a confirmed track keeps
                # counting, an unconfirmed one keeps accumulating (it is
                # reset by a miss in _advance_misses).
                track["seen"] += 1
                track["misses"] = 0
            else:
                track_id = self._next_id
                self._next_id += 1
                self._tracks[track_id] = {
                    "det": det, "seen": 1, "misses": 0,
                }
                matched.add(track_id)

        return self._advance_misses(matched)

    def _advance_misses(self, matched: set) -> List[object]:
        """Increment misses, reset unconfirmed tracks, prune, report."""

        for track_id, track in self._tracks.items():
            if track_id not in matched:
                track["misses"] += 1
                # "Keep alive 3 frames" is about *consecutive* frames: a
                # miss interrupts the streak for a not-yet-confirmed track.
                if track["seen"] < self.confirm_frames:
                    track["seen"] = 0
        for track_id in [
            tid for tid, track in self._tracks.items()
            if track["misses"] > self.miss_hold
        ]:
            del self._tracks[track_id]
        return [
            track["det"] for track in self._tracks.values()
            if track["seen"] >= self.confirm_frames
        ]

    def reset(self) -> None:
        """Forget all tracks (e.g. on a map change)."""

        self._tracks.clear()
        self._next_id = 0

    def track_count(self) -> int:
        return len(self._tracks)


__all__ = ["MobTracker"]
