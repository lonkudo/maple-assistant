"""Minimap-driven movement worker.

The worker consumes screenshots produced by ``capture_worker``.  It finds the
yellow player marker in the top-left minimap, estimates a nearby connector to
an upper platform, and emits short, conservative key taps through the shared
key sender.  It contains no global keyboard hooks and never sends keys itself;
the sender remains responsible for checking/focusing the configured window.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import logging
import queue
import random
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

import numpy as np
from PIL import Image

from marker_detector import (
    DiamondSizeTracker,
    detect_red_diamonds,
    detect_yellow_diamond,
)
from patrol_control import CoordinateLayout, _layer_present_actions

from combat_coordination import AttackStateFile, PatrolStateFile, RopeStateFile
from channel_switch import channel_switch_procedure
from channel_routing import normalize_channel, plan_next_channel
from config_store import config_section_file
from game_chat import send_game_chat_message


LOG = logging.getLogger(__name__)

OTHER_PLAYER_INITIAL_PAUSE_SECONDS = 180.0
OTHER_PLAYER_REQUEST_MIN_SECONDS = 60.0
OTHER_PLAYER_REQUEST_MAX_SECONDS = 180.0
OTHER_PLAYER_MONITOR_SECONDS = 180.0
OTHER_PLAYER_PRESENCE_POLL_SECONDS = 1.0


# Stall recovery is only valid at the rope itself.  Keeping this threshold in
# one place prevents the stall detector and its recovery action from drifting
# apart and turning a long rope approach into repeated jump-climb attempts.
ROPE_STALL_ALIGNMENT_RANGE = 0.03

# A rope's X is stored as a diamond-relative coordinate and re-projected through
# the LIVE minimap layout on every frame.  One pixel of error in the measured
# yellow diamond moves that projection by diamond_distance/analysis_width - on a
# 87px minimap with a ~3.8 diamond offset that is ~0.044 per pixel, so a half
# pixel of diamond noise slides the rope target by ~0.02: more than the rope's
# own 0.018 jump band.  Observed consequence: the walk chased a target that slid
# under it, and the lateral climb jump picked the side of the *moved* target -
# the character sat 0.008 to the right of the rope and was sent Alt+right, i.e.
# further away, instead of Alt+left.  One rope phase therefore keeps the target
# it locked on and only adopts a sample that really moved.
ROPE_TARGET_JITTER_BAND = 0.02
# The locked target follows its samples with a slow exponential average, so
# one-pixel diamond noise moves it by a small fraction of that noise instead of
# swinging the whole band, while a real move (> the jitter band) re-locks it
# immediately.  0.15 keeps the residual swing of a +-0.016 sample noise under
# 0.005 - a third of the raw jitter, well inside the rope band.
ROPE_TARGET_SMOOTHING_ALPHA = 0.15
# Lateral jump direction dead band.  Below this the character is treated as
# aligned and keeps the side it approached from, instead of flipping on marker
# quantization noise.
ROPE_JUMP_DIRECTION_DEAD_BAND = 0.002
# Endpoint reversals need a real neutral input tick: Maple can otherwise
# consume the new direction when it arrives in the old key-up poll slice.
DIRECTION_SWITCH_NEUTRAL_GAP_SECONDS = 0.10
# Jump records are X-precise, but the marker can be one or more vertical
# minimap pixels away while grabbing/climbing a rope.  Keep a little more
# than one X pixel and about two Y pixels: narrower than the earlier loose
# window, without becoming smaller than the diamond's capture grid.
JUMP_POINT_X_TOLERANCE = 0.008
JUMP_POINT_Y_TOLERANCE = 0.015
# A left/right point that lands on a horizontal platform needs only a brief
# post-Alt guard, then two stable 5-FPS samples before its Up claim releases.
# A point that really grabbed a rope hands over earlier, on its first upward
# marker advance, to the normal climb state machine.
JUMP_POINT_LANDING_GUARD_SECONDS = 0.25
JUMP_POINT_LANDING_STABLE_FRAMES = 2
# A recorded jump point is a trigger for a LEG, not for every entry into its
# X/Y zone.  The zone covers about one X pixel and two Y pixels, so a
# character that jumps, lands, walks back over the same spot, or is climbed past
# re-enters it constantly - the field report is a 右跳 recorded at layer2 that
# mounted the rope and then fired the same 右跳 again on another run, throwing
# the character off the rope it had just grabbed.  One point therefore fires at
# most once per leg (see ``_jump_point_leg_key``) and once per pass from a climb.
# This cooldown is only the backstop for a marker that flickers across the zone
# edge (two shared-capture frames at 5 FPS); it stays short so it can never
# swallow a genuine crossing on the next leg.
JUMP_POINT_REFIRE_COOLDOWN_SECONDS = 0.4

# Movement-thread stall watchdog.  When patrol input is armed and this worker
# has consumed no frame for STALL_WATCHDOG_SECONDS, its own stack is reported
# as an error. Other workers commonly sit in timed Event/Queue waits, so their
# snapshots are diagnostic-only and must never be presented as failures.
STALL_WATCHDOG_INTERVAL_SECONDS = 5.0
STALL_WATCHDOG_SECONDS = 20.0

# 自动重连: the reconnect owns the keyboard for its whole sequence and the login screens hide the
# minimap, so the character can be anywhere when patrol input comes back.  The falling edge of
# ``reconnect_active_event`` arms ONE route check, which then waits up to this long for a usable
# marker reading before leaving the decision to the normal per-frame floor verifier.
ROUTE_CHECK_AFTER_RECONNECT_SECONDS = 8.0
# Same structure-confidence gate the floor detection itself uses before it trusts the
# scroll-compensated world Y (see ``_detect_floor_all`` / ``_on_first_layer``).
ROUTE_CHECK_MIN_STRUCTURE_CONFIDENCE = 0.12

# Reconnect-only recovery for a character standing on an upper platform that
# refuses the normal Alt+Down chord.  The minimap Y axis grows downward.
RECONNECT_DROP_STALLED_ATTEMPTS = 3
RECONNECT_DROP_Y_PROGRESS = 0.006
RECONNECT_DROP_EDGE_HOLD_SECONDS = 5.0

# Landing confirmation for the drop-to-route return (``_drop_landing_floor``).
# A drop can land the character on a floor whose recorded Y band cannot contain
# the reading, because the marker stands away from the recorded row - the
# operator's own map: layer1's points were saved at 0.676829 while the
# character stands at 0.713415 further down the same platform.  Ordinary patrol
# already resolves that case with its marker-only fallbacks, so the drop uses
# them too, but only after real drop evidence (an Alt+Down chord was sent AND
# the marker actually moved down) and only while the reading is SETTLED: a
# character falling THROUGH the floor sweeps the marker down the minimap by
# ``fall_marker_y_gain`` per frame (his 14:38 log ran 0.372 -> 0.397 -> 0.409 ->
# 0.445 -> 0.482) and must never be read as a landing.  Two settled frames hold
# the same floor at the shared 5 FPS cadence.
DROP_ARRIVAL_CONFIRM_FRAMES = 2

# 站桩攻击 records the player's current marker only for the active session.
# It is never persisted and is independent of the recorded route/layer data.
# The stationary target remains the recorded X.  Its accepted resting window
# is 0.016 wide (the former +/-0.008 band), shifted 0.002 toward the selected
# final facing so the short facing tap does not immediately walk the character
# back through the target.
STATIONARY_ATTACK_X_TOLERANCE = 0.008
STATIONARY_ATTACK_FACING_ZONE_SHIFT = 0.002
# The standing position has a Y half too, and it uses the same anchor band:
# marker Y moves by a minimap pixel on its own, and a jump spent on that jitter
# walks the character off its platform (observed in the field).  One single
# jump is also not enough when the character really is displaced - the attempt
# is re-armed while the mismatch lasts, one jump per window, with a slower
# cadence after the first burst so a spot that a jump cannot reach never turns
# into an endless jump loop.
STATIONARY_ATTACK_Y_TOLERANCE = 0.006
# A single minimap capture can place the diamond one pixel away from its
# standing position.  It must never make 站桩攻击 jump: require the same
# out-of-band Y direction in two consecutive captures before a recovery jump
# is armed.  At the shared 5 FPS cadence this still reacts to a real fall in
# about 0.2 seconds.
STATIONARY_ATTACK_Y_CONFIRM_FRAMES = 2
# Used only to decide whether the marker is still on the anchor platform. It
# is intentionally wider than the exact launch-position tolerance: minimap
# pixel jitter must not suppress attacks during final X correction.
STATIONARY_ATTACK_SAME_LAYER_Y_TOLERANCE = 0.020
# Stationary recovery begins close to its anchor.  A normal patrol hold is
# deliberately long, but it makes a near-anchor correction overshoot before
# the next capture arrives.  Keep this distinct, short hold for stand-still
# mode only.
STATIONARY_ATTACK_RECOVERY_HOLD_SECONDS = 0.12
# Inside the small-step band the correction is the tiny step that finishes the
# last pixels onto the 桩.  It is the SAME arbiter motion as the longer walk
# back - a direction hold followed by the attack that belongs to the correction
# (see ``perform_stationary_step``) - so only the hold below differs.
STATIONARY_ATTACK_NEAR_RECOVERY_HOLD_SECONDS = 0.03
# Space between two corrections.  The operator asked for a gap of about 300ms
# between them: corrections pressed back to back read as continuous walking.  A
# correction now carries its own attack, so this spacing is also the attack rate
# while the character is correcting; on a map whose minimap is coarse enough
# that every correction needs the longer hold, that is roughly one attack per
# 0.3s until the marker is back on the band.
STATIONARY_ATTACK_NEAR_CORRECTION_INTERVAL_SECONDS = 0.30
# X recovery around the temporary 桩.  Every correction - the tiny 30ms step and
# the longer walk back - is ONE arbiter motion that carries ONE attack at its
# end (see ``perform_stationary_step``).  That combination is what the operator
# asked for after the field run showed the character correcting its position
# with no attack at all: sent as an ordinary walk hold, the correction either
# deferred the fixed cadence (direction handoff) or blocked it outright (the
# exclusive recovery), so the beats that landed inside a correction were lost.
# With the attack inside the correction, a correction can never eat one.
#
# The arrival band is the exact temporary anchor.  Inside this wider final
# approach zone, recovery is owned by the arbiter and carries its own attack;
# the marker is still considered arrived only inside
# ``STATIONARY_ATTACK_X_TOLERANCE``.
STATIONARY_ATTACK_X_HOLD_TOLERANCE = 0.020
# Outside this band the character is plainly away from the stake and must
# walk back normally.  The short correction-plus-attack motion is reserved
# for the final approach only; using it for a rope-top or another platform
# created a visible tiny-step/attack loop before the stake was reached.
STATIONARY_ATTACK_FINAL_APPROACH_X_RANGE = 0.030
# How long the 朝向 tap holds its direction.  Deliberately as short as the tiny
# step: the tap is a TURN, not a move, and a longer hold walks the character out
# of the anchor band (at 0.10s it travels about a minimap pixel, which on a
# small minimap is already outside the band) - the correction then has to walk
# it back, which turns it again and re-arms the facing, i.e. the face/walk
# twitch.  The operator asked for this 30ms.
STATIONARY_ATTACK_FACING_HOLD_SECONDS = 0.03
# A facing tap moves the character a fraction of a minimap pixel.  Turn from
# the *opposite* side of the accepted band, but keep this much room inside the
# edge so a coarse minimap step cannot immediately put the marker outside the
# band before the tap lands.
STATIONARY_ATTACK_FACING_TURN_INSET = 0.003
# A facing tap is not trusted until fresh captures prove that it left the
# marker in the accepted band.  This keeps the fixed cadence out of the same
# arbiter window as the final turn.
STATIONARY_ATTACK_FACING_CONFIRM_FRAMES = 2
# A return climb may be confirmed while the marker is still at the rope top.
# Before ordinary stake recovery begins, use one deliberate lateral hold toward
# the temporary anchor so the character steps off the rope instead of issuing
# short walks that the rope swallows.
STATIONARY_RETURN_ROPE_DISMOUNT_HOLD_SECONDS = 0.40
# 双向 alternates the final stationary facing after this many settled
# minimap frames. At the shared 5 FPS capture cadence this is about 16s.
STATIONARY_ATTACK_BILATERAL_FACING_FRAMES = 80
STATIONARY_ATTACK_Y_JUMP_GAP_SECONDS = 1.5
STATIONARY_ATTACK_Y_BURST_JUMPS = 4
STATIONARY_ATTACK_Y_RETRY_SECONDS = 10.0
# The stand-still pickup run is a complete left -> right -> anchor circuit.
# It uses the same timing vocabulary as the optional motion controls, but is
# kept in this worker because it owns the temporary stationary anchor.
# 捡东西 is configured in MINUTES on its UI row ("m") except for the short
# end, which is displayed in seconds.  The trigger range is 10s .. 30m.
STATIONARY_PICKUP_MIN_INTERVAL_SECONDS = 10.0
STATIONARY_PICKUP_MAX_INTERVAL_SECONDS = 1800.0  # 30.0m

# A saved Left/Right endpoint that sits a fraction of a pixel outside the
# walkable platform (wall/platform edge, or a residual projection error) can
# never satisfy the arrival band, so the walk holds its key until the self-
# rescue restarts the whole patrol.  Frames spent within a few times the band
# count down to a forced turn instead.
ENDPOINT_ARRIVAL_TIMEOUT_FRAMES = 19
ENDPOINT_ARRIVAL_NEAR_MARGIN = 4.0
# Far-away stalls need their own bound: a saved endpoint beyond a wall (or a
# marker frozen by a movement-locking buff) never enters the near zone, so the
# character walks into the edge forever ("patrol keeps walking left") until the
# self-rescue restarts the whole patrol.  A real walk always CLOSES the
# distance, so only a frame run without progress counts.
ENDPOINT_NO_PROGRESS_FRAMES = 38


class KeySender(Protocol):
    """Small interface implemented by the integration's WindowKeySender."""

    dry_run: bool

    def press(self, key: str, duration: float = 0.0) -> Any: ...


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class MinimapObservation:
    """Coordinates are normalized within the cropped minimap (0..1)."""

    player: Optional[Point]
    target: Optional[Point]
    confidence: float
    minimap_box: tuple[int, int, int, int]
    platform_y: Optional[float] = None
    action: str = "unknown"
    marker_pixel_size: Optional[tuple[int, int]] = None
    analysis_size: Optional[tuple[int, int]] = None
    world_y_diamonds: Optional[float] = None
    structure_confidence: float = 0.0
    scroll_y_diamonds: float = 0.0


def _dispatched_position_matches(
    dispatched: Any,
    frame_sequence: int,
    minimap_region: tuple[float, float, float, float],
) -> bool:
    """Whether a secondary marker reading belongs to this exact analysis.

    Before the detected minimap region is available, CharacterWorker uses a
    broad fallback crop. A yellow object on that crop's edge can report a
    confident ``y=0``. Never let a reading from another frame/region—or a
    clipped border component—overwrite MovementWorker's current observation.
    """

    if (dispatched is None
            or getattr(dispatched, "x", None) is None
            or getattr(dispatched, "y", None) is None
            or float(getattr(dispatched, "confidence", 0.0)) < 0.5):
        return False
    if getattr(dispatched, "frame_sequence", None) != frame_sequence:
        return False
    source_region = getattr(dispatched, "minimap_region", None)
    if not isinstance(source_region, (tuple, list)) or len(source_region) != 4:
        return False
    if any(
        abs(float(source) - float(current)) > 1e-9
        for source, current in zip(source_region, minimap_region)
    ):
        return False
    x = float(dispatched.x)
    y = float(dispatched.y)
    return 0.0 < x < 1.0 and 0.0 < y < 1.0


@dataclass(frozen=True)
class MovementDecision:
    key: Optional[str]
    reason: str
    duration: float = 0.0


@dataclass(frozen=True)
class RopeMovementPlan:
    """Explicit separation between travelling to the rope and climbing it."""

    stage: str
    current: Optional[Point]
    target_x: float
    gap: Optional[float]
    decision: MovementDecision


@dataclass(frozen=True)
class PositionMovementPlan:
    """Result of travelling toward one calibrated patrol boundary."""

    stage: str
    current: Optional[Point]
    target: Point
    gap: Optional[float]
    reached_or_crossed: bool
    decision: MovementDecision


def _layer_point_ys(layer: Any) -> list[float]:
    values = []
    for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
        point = layer.get(point_name)
        if isinstance(point, dict) and "y" in point:
            values.append(float(point["y"]))
    return values


# How much of a layer's Y tolerance is allowed ABOVE its topmost recorded point ("the upper band").
#
# The operator asked for a narrower upper edge (2026-09-18: "narrow down layer upper band a little bit,
# make it 0.7"): a band that reaches far above a floor makes a character that is still above it (on the
# rope, dropping in) read as standing on that floor.  On his two floors 0.02 x 0.7 = 0.014 = 1.2 px of
# the 86 px minimap (before: 0.02 = 1.7 px), still above the marker's own 1 px quantisation, so a stand
# at the TOP end of a platform is not lost.
#
# The down side (``tolerance / 3``) and the current-floor grace (``LAYER_CURRENT_Y_GRACE``) are
# unchanged, and so is the CLIMB arrival test - it asks about the floor ABOVE and enters that band
# through its LOWER edge.
LAYER_UP_REACH_FACTOR = 0.7

# Last-resort gap for a single-supporter layer (or a ``layer_y``-only layer)
# whose recording carries no layout at all, so one marker row cannot be derived.
# The normal gap is ``max(tolerance / 3, 1 / analysis_height)`` - about one
# marker pixel; see ``_layer_marker_row``.
LAYER_SINGLE_SUPPORTER_GAP_FALLBACK = 0.002

# Consecutive "no recorded floor detected" frames a climb-arrival confirmation
# tolerates while the climb still owns Up.  At the shared 5 FPS cadence the
# operator's own jump arc keeps the marker one marker row above the platform for
# 1-2 frames (13:58 log: layer2's row 0.653571 / world 3.546 alternates with
# 0.617857 / world 2.71 on every Alt+Up attempt).  Treating each of those frames
# as "not arrived" zeroed the streak, so arrival could never be confirmed and the
# climb restarted forever.  A longer blank run is real evidence and still resets.
CLIMB_ARRIVAL_BLANK_FRAMES_TOLERATED = 3


def _layer_marker_row(layer: Any) -> Optional[float]:
    """One marker row (1 px) in normalised minimap Y, from the recorded layout.

    The operator's convention for a layer that has a single supporter (rope
    only, left-most only, or right-most only) is the one-sided band
    ``[y, y + small_gap]``.  ``small_gap`` must never be smaller than one marker
    row: the yellow diamond's centre quantises to ``1 / analysis_height``
    (0.007143 on the 114x140 minimap, where every recorded Y is a half-pixel
    centre such as 0.753571 or 0.510714), so the old fixed 0.002 - 0.28 px -
    could only match a reading that landed on the recorded row exactly.  The
    layout travels with every recorded point, so the row comes from the
    recording instead of a constant.
    """

    if not isinstance(layer, dict):
        return None
    candidates: list[Any] = [
        layer.get(name)
        for name in ("left_most_pos", "rope_pos", "right_most_pos")
    ]
    jump_points = layer.get("jump_points")
    if isinstance(jump_points, list):
        candidates.extend(jump_points)
    for point in candidates:
        coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
        if not isinstance(coordinate, dict):
            continue
        layout = coordinate.get("recorded_layout")
        if not isinstance(layout, dict):
            continue
        try:
            height = float(layout.get("analysis_height"))
        except (TypeError, ValueError):
            continue
        if height >= 1.0:
            return 1.0 / height
    return None


def _layer_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    """Layer band from its saved recording envelope.

    band = (uppermost point Y - tolerance * ``LAYER_UP_REACH_FACTOR``,
            lowermost point Y + tolerance / 3).
    A layer whose points span a Y range (wide platform / minimap
    perspective) is fully detected while standing anywhere on the
    platform; the old mean +- tolerance band excluded the platform ends,
    so the marker at an edge flipped between floors every frame and the
    route never advanced to the next layer.  The tolerance is applied
    ONLY upward (above the topmost point, where the climb/drop arrives)
    and NOT below the lowermost point, so the band does not reach into
    the layer BELOW - adjacent floors' bands overlap less.  Falls back
    to ``layer_y`` when the layer has no recorded points (still the mean
    band then).
    """
    # ``PatrolController`` materialises these bounds whenever a supporter is
    # recorded, removed, or re-projected.  Runtime must use that one saved
    # decision rather than independently reassembling a slightly different
    # band from endpoint samples on each worker path.  The old calculation is
    # retained only for profiles created before explicit bands were added.
    saved = layer.get("layer_band") if isinstance(layer, dict) else None
    # ``None`` is an explicit result of deleting the last supporter.  It is
    # not a legacy omission and must not fall through to stale ``layer_y``.
    if isinstance(layer, dict) and "layer_band" in layer and saved is None:
        return None
    if isinstance(saved, dict):
        try:
            upper = float(saved["y_upper"])
            lower = float(saved["y_lower"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if upper <= lower:
                return upper, lower
    values = _layer_point_ys(layer)
    if not values and isinstance(layer, dict) and "layer_y" in layer:
        values = [float(layer["layer_y"])]
    if not values:
        return None
    effective_tolerance = max(0.0, float(tolerance))
    if len(values) == 1:
        # A rope-only (or otherwise one-point) layer is a reference point,
        # not a horizontally sampled platform.  Giving it the ordinary
        # +/- tolerance band makes it overlap the actual floor above/below
        # and steals layer identity during a return.  Its narrow, one-sided
        # band follows the operator's convention: [recorded_y, y + small_gap].
        # ``jump_points`` are intentionally not part of ``_layer_point_ys``.
        #
        # ``small_gap`` is the same "one third below the base" the multi-point
        # branch uses, but never smaller than one marker row: a narrower band
        # can only match the exact recorded row, which is what made a character
        # walking that very floor read "matches no band" (13:16 log: layer1
        # band (0.753571, 0.755571) while the marker read 0.796429 / 0.767857 /
        # 0.732143 on that same floor).
        only_y = values[0]
        gap = effective_tolerance / 3.0
        marker_row = _layer_marker_row(layer)
        if marker_row is not None:
            gap = max(gap, marker_row)
        if gap <= 0.0:
            gap = LAYER_SINGLE_SUPPORTER_GAP_FALLBACK
        return only_y, only_y + gap
    # The tolerance above the visually highest point covers climb/drop arrival
    # movement, scaled by the operator's upper-band factor.  Only one third is
    # allowed below the confirmed layer base: enough for OpenCV/marker
    # quantization noise without making the band unnecessarily reach toward the
    # layer below.
    return (
        min(values) - effective_tolerance * LAYER_UP_REACH_FACTOR,
        max(values) + effective_tolerance / 3.0,
    )


def _has_layer_y_supporter(layer: Any) -> bool:
    """Whether a layer has any recorded floor supporter.

    ``layer_y`` is a legacy cached summary, not the recording itself.  A
    rope-only layer is still a valid floor and must be discoverable from its
    ``rope_pos`` even if that cache was not written (for example after a
    partial/manual recording).  Jump points deliberately do not count.
    """

    if not isinstance(layer, dict):
        return False
    tolerance = float(layer.get("y_tolerance", 0.020000))
    return _layer_y_band(layer, tolerance) is not None


def _coherent_observed_world_points(
    layer: Any,
) -> list[tuple[float, float]]:
    """Return ``(x, observed_world_y)`` points that describe a real slope.

    Recording deliberately keeps a canonical ``world_y`` on every point so
    one horizontally repeating platform cannot create several fake floors.
    It also stores the raw ``observed_world_y`` and adaptive diamond-space Y.
    When those two measurements move together, the change is real geometry
    (for example a left-high/right-low stair layer), not a phase-correlation
    alias. Such coherent readings may safely form a world-Y interval.
    """

    points: list[tuple[float, float, float]] = []
    for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
        point = layer.get(point_name) if isinstance(layer, dict) else None
        coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
        if (not isinstance(point, dict)
                or not isinstance(coordinate, dict)
                or "x" not in point
                or "observed_world_y" not in point
                or "y_diamond" not in coordinate
                or float(point.get("tracking_confidence", 0.0)) < 0.12):
            continue
        points.append((
            float(point["x"]),
            float(point["observed_world_y"]),
            float(coordinate["y_diamond"]),
        ))
    if len(points) < 2:
        return []
    offsets = [world_y - diamond_y for _, world_y, diamond_y in points]
    # A genuine slope changes local diamond Y and world Y by the same amount.
    # Allow sub-diamond capture noise, but reject a repeated-platform alias.
    if max(offsets) - min(offsets) > 0.35:
        return []
    return [(x, world_y) for x, world_y, _ in points]


def _layer_world_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    """World-Y band from recorded point world-Ys (same rule as Y:

    tolerance applies only above the topmost point, not below the
    lowermost point, so adjacent floors' bands overlap less."""
    saved = layer.get("layer_band") if isinstance(layer, dict) else None
    if isinstance(layer, dict) and "layer_band" in layer and saved is None:
        return None
    if isinstance(saved, dict):
        try:
            upper = float(saved["world_y_upper"])
            lower = float(saved["world_y_lower"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if upper <= lower:
                return upper, lower
    coherent_points = _coherent_observed_world_points(layer)
    values = [world_y for _, world_y in coherent_points]
    if not coherent_points:
        for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
            point = layer.get(point_name)
            if isinstance(point, dict) and "world_y" in point:
                values.append(float(point["world_y"]))
    if not values and isinstance(layer, dict) and "layer_world_y" in layer:
        values = [float(layer["layer_world_y"])]
    if not values:
        return None
    return min(values) - tolerance, max(values)


def _layer_world_anchor_at_x(layer: Any, player_x: Optional[float]) -> Optional[float]:
    """Return the recorded world-Y expected at X on a flat or stair layer."""

    points = sorted(_coherent_observed_world_points(layer))
    if player_x is not None and points:
        x = float(player_x)
        if x <= points[0][0]:
            return points[0][1]
        if x >= points[-1][0]:
            return points[-1][1]
        for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
            if left_x <= x <= right_x:
                span = right_x - left_x
                if span <= 1e-9:
                    return (left_y + right_y) / 2.0
                ratio = (x - left_x) / span
                return left_y + (right_y - left_y) * ratio
    if isinstance(layer, dict) and "layer_world_y" in layer:
        return float(layer["layer_world_y"])
    return None


# The marker's own pixel quantisation, expressed in normalised minimap Y.
#
# Measured on the operator's layer3_error frame (his 105x86 minimap): the diamond was found cleanly
# (pixel box 75,28-81,34) but its centre Y 0.3547 sat 0.0050 BELOW the band of the floor it was
# standing on (0.3114..0.3497) - 0.43 px - and the frame was reported as "no layer".  One pixel of that
# minimap is 1/86 = 0.0116, which is larger than the whole downward side of a band (_layer_y_band adds
# only ``tolerance / 3`` = 0.0067 below the lowermost recorded point), so a character standing at the
# low end of the floor it is already patrolling reads as "none": "LAYER DEBUG: now on none
# (player_y=0.180233)" alternating with "now on layer2 (0.168605)" in the operator's log is the same
# 0.0050 miss.  That "none" then feeds ``on_rope`` and the route resync.
#
# The grace below is applied ONLY to the question "am I still on the floor the route is on"
# (``_detected_layer``).  The band lookup used for CLIMB ARRIVAL stays strictly zero-below on purpose:
# a band that reaches down toward the floor below makes a climb look arrived while the character is
# still on the rope, and releasing Up there is what pulled the character off the rope ("the old
# nearest-anchor rule switched at the midpoint between floors, released Up while the character was
# still on the rope, and then horizontal patrol pulled it off").
LAYER_CURRENT_Y_GRACE = 0.012

# A nearest-recorded-Y answer is a small visual-quantisation fallback, not a
# substitute for floor identity after a fall.  Five marker rows comfortably
# covers an unsampled end of a bench/stair platform (the use case for this
# fallback), while rejecting the 0.1429-wide guess that turned a fall through
# layer2/layer1 back into layer3 patrol.
LAYER_NEAREST_FLOOR_MAX_DISTANCE = 0.050

# How long the planned descent to the route's first floor may keep the layer state before the normal
# recovery takes over again.  The descent owns every vertical move (see ``_resync_route_layer``), so it
# must yield as soon as it is not making progress: a character that cannot drop any further (no platform
# edge under it), or one a monster knocked around, must not sit there sending Alt+Down forever while the
# knock-down and return-to-route logic wait.  A healthy descent needs one chord per floor (about 1-2 s
# each), so this is generous.
DROP_TO_FIRST_MAX_SECONDS = 10.0


def _layer_y_distance(layer: Any, player_y: float) -> Optional[float]:
    """Distance from the marker Y to the floor's nearest RECORDED position.

    The one measure that answers "which floor is the character standing on" when two bands overlap: how
    far the marker is from a spot that floor was recorded at.  ``layer_y`` (the marker Y of whichever spot
    was recorded first) is NOT that measure - on a stair/bench floor it is one end of the range.
    """

    values = _layer_point_ys(layer)
    if not values and isinstance(layer, dict) and "layer_y" in layer:
        values = [float(layer["layer_y"])]
    if not values:
        return None
    return min(abs(player_y - value) for value in values)


def _layer_y_candidates(player_y: float, layers: dict[str, Any]) -> list[str]:
    """All marker-Y matches ordered from the nearest recorded floor position.

    Two floors' bands can overlap (their recorded points are close, or one floor's platform reaches the
    other's height), and then the ORDER decides.  It used to be the distance to the layer's legacy
    single ``layer_y`` - the marker Y of whatever spot was recorded first - which says nothing about the
    positions a floor actually patrols: on a stair/bench floor the base is one end of the range, so a
    marker sitting exactly on ANOTHER floor's recorded point could still rank that other floor first.
    The operator's 13:26 log is that case: the character started on layer1 at player_y=0.676829 (= layer1's
    own recorded position, distance 0) and the worker answered layer2, anchored the session to layer2's
    world Y and patrolled layer2's points.

    Ranking by the nearest RECORDED POSITION measures what the question is really about - which floor's
    recorded stance is closest to where the marker is drawn.  Ties keep the old alphabetical order, so
    ``layer1`` still wins over ``layer2``.
    """

    candidates: list[tuple[float, str]] = []
    for name, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None or not band[0] - 1e-9 <= player_y <= band[1] + 1e-9:
            continue
        distance = _layer_y_distance(layer, player_y)
        if distance is None:
            continue
        candidates.append((distance, name))
    return [name for _, name in sorted(candidates)]


def detect_layer_by_y(
    player_y: float,
    layers: dict[str, Any],
) -> Optional[str]:
    """Return the nearest layer whose recorded-point band contains Y."""

    candidates = _layer_y_candidates(player_y, layers)
    return candidates[0] if candidates else None


def detect_layer_by_world_y(
    world_y: float,
    layers: dict[str, Any],
) -> Optional[str]:
    """Return nearest layer using scroll-compensated map-structure Y."""

    candidates = []
    for name, layer in layers.items():
        if not isinstance(layer, dict) or "layer_world_y" not in layer:
            continue
        tolerance = float(layer.get("world_y_tolerance", 0.75))
        band = _layer_world_y_band(layer, tolerance)
        if band is None:
            continue
        band_min, band_max = band
        if band_min - 1e-9 <= world_y <= band_max + 1e-9:
            reference_y = float(layer.get(
                "layer_world_y", (band_min + band_max) / 2.0
            ))
            candidates.append((abs(world_y - reference_y), name))
    return min(candidates)[1] if candidates else None


def _layer_number(name: str) -> int:
    """Trailing floor number of a layer name (``layer12`` -> 12)."""
    match = re.search(r"(\d+)$", name)
    return int(match.group(1)) if match else 0


# Patrol ranges already reported as inverted, so the warning is said once per pair instead of on
# every frame (``_sync_patrol_controller`` re-slices continuously).
_INVERTED_RANGES_REPORTED: set[tuple[str, str]] = set()

# (floor the world-Y tracker answered, lower floor the minimap marker allows) pairs already reported.
# A landing resolution runs on every frame while the fall is pending, so the same disagreement must be
# said once instead of flooding the log.
_LANDING_CAP_REPORTED: set[tuple[str, str]] = set()


def _canonical_patrol_range(
    patrol_start_layer: Optional[str], patrol_end_layer: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Return the patrol range in this code's order: lowest floor -> highest floor.

    A recording profile can hold the pair the other way round (observed from the operator's log:
    ``route=layer2 -> layer1``).  Both names describe the SAME set of floors, but the numeric slice
    then matches nothing, ``_route_layers`` comes out empty and patrol stands still with no
    explanation at all.  Swap it and say so loudly, once per pair.
    """

    if not patrol_start_layer or not patrol_end_layer:
        return patrol_start_layer, patrol_end_layer
    if _layer_number(patrol_start_layer) <= _layer_number(patrol_end_layer):
        return patrol_start_layer, patrol_end_layer
    if (patrol_start_layer, patrol_end_layer) not in _INVERTED_RANGES_REPORTED:
        _INVERTED_RANGES_REPORTED.add((patrol_start_layer, patrol_end_layer))
        LOG.warning(
            "巡逻范围 %s -> %s 方向相反（起点在终点上方，切片结果为空，巡逻会原地站立）；"
            "已按 %s -> %s 处理，请在界面上重新确认范围",
            patrol_start_layer, patrol_end_layer, patrol_end_layer, patrol_start_layer,
        )
    return patrol_end_layer, patrol_start_layer


def _slice_patrol_range(
    layers: list[str],
    patrol_start_layer: Optional[str],
    patrol_end_layer: Optional[str],
) -> list[str]:
    """Keep only the contiguous floor range [start .. end] of ``layers``.

    ``layers`` must already be sorted bottom-up by floor number.  An unset
    bound (or one absent from the recorded layers) defaults to the bottom /
    top recorded floor, preserving the legacy patrol route.  A single floor
    is allowed (start == end).
    """
    if not layers:
        return layers
    patrol_start_layer, patrol_end_layer = _canonical_patrol_range(
        patrol_start_layer, patrol_end_layer
    )
    numbers = [_layer_number(name) for name in layers]
    start_number = (
        _layer_number(patrol_start_layer)
        if patrol_start_layer else numbers[0]
    )
    end_number = (
        _layer_number(patrol_end_layer)
        if patrol_end_layer else numbers[-1]
    )
    return [
        name for name, number in zip(layers, numbers)
        if start_number <= number <= end_number
    ]


def _patrol_range_numbers(
    layers: list[str],
    patrol_start_layer: Optional[str],
    patrol_end_layer: Optional[str],
) -> tuple[int, int]:
    """Floor-number bounds of the patrol range (defaults: bottom/top)."""

    patrol_start_layer, patrol_end_layer = _canonical_patrol_range(
        patrol_start_layer, patrol_end_layer
    )
    numbers = [_layer_number(name) for name in layers]
    start = (
        _layer_number(patrol_start_layer)
        if patrol_start_layer else (numbers[0] if numbers else 0)
    )
    end = (
        _layer_number(patrol_end_layer)
        if patrol_end_layer else (numbers[-1] if numbers else 0)
    )
    return start, end


def _move_to_boundary(
    observation: MinimapObservation,
    target: Point,
    *,
    boundary: str,
    horizontal_tolerance: float = 0.010,
    movement_hold_seconds: float = 2.0,
    minimum_confidence: float = 0.55,
) -> PositionMovementPlan:
    """Move at fixed speed until a left/right boundary is reached or crossed."""

    player = observation.player
    if player is None or observation.confidence < minimum_confidence:
        return PositionMovementPlan(
            f"move-to-{boundary}", player, target, None, False,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )
    gap = target.x - player.x
    if boundary == "left-most":
        reached = player.x <= target.x + horizontal_tolerance
        direction = "left"
    elif boundary == "right-most":
        reached = player.x >= target.x - horizontal_tolerance
        direction = "right"
    else:
        raise ValueError(f"unknown boundary: {boundary}")
    decision = (
        MovementDecision(None, f"{boundary} reached or crossed")
        if reached else
        MovementDecision(direction, f"fixed movement toward {boundary}", movement_hold_seconds)
    )
    return PositionMovementPlan(
        f"move-to-{boundary}", player, target, gap, reached, decision
    )


def move_to_left_most(
    observation: MinimapObservation,
    target: Point,
    **movement_options: Any,
) -> PositionMovementPlan:
    return _move_to_boundary(
        observation, target, boundary="left-most", **movement_options
    )


def move_to_right_most(
    observation: MinimapObservation,
    target: Point,
    **movement_options: Any,
) -> PositionMovementPlan:
    return _move_to_boundary(
        observation, target, boundary="right-most", **movement_options
    )


def move_towards_rope(
    observation: MinimapObservation,
    rope_x: float,
    near_range: float,
    inner_range: float = 0.0,
    under_rope_tolerance: float = 0.008,
    allow_climb: bool = True,
    **movement_options: Any,
) -> RopeMovementPlan:
    """Move into the inner rope band, then jump inward from within it.

    Three behavior zones around the rope:

    - **Right on the rope** (|gap| <= ``under_rope_tolerance``, default
      +-0.008 minimap units): jump straight up (``jump_climb_up``) - a
      left/right chord from directly under the rope pushes the character past
      it.
    - **Inner band** (under_rope < |gap| <= ``inner_range``): the climb
      attempt jumps left/right toward the rope side (``jump_climb_<side>``).
      When ``allow_climb=False`` (fresh YOLO owns the jump decision) the plan
      only creeps toward the rope center with a tiny random step so it never
      races the YOLO jump.
    - **Honey zone** (inner band < |gap| <= ``near_range``): tiny RANDOM
      steps toward the rope - never a big walk (it would overshoot the rope)
      and never a jump (the jump gate is not satisfied yet).
    - **Outside the honey zone**: big walking toward the honey-zone edge
      (full fixed holds, shortened only inside the final edge-calculation
      zone so the character does not overshoot the band).

    The tiny-step bounds come from ``tiny_step_min_seconds`` /
    ``tiny_step_max_seconds`` in ``movement_options`` (defaults 0.05 / 0.15 s).
    """

    player = observation.player
    if player is None:
        return RopeMovementPlan(
            "detect", None, rope_x, None,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )
    near_range = max(0.0, float(near_range))
    inner_range = max(0.0, float(inner_range))
    under_rope_tolerance = max(0.0, min(float(under_rope_tolerance), inner_range or 1.0))
    # Without an explicit inner gap the approach band is the single gate.
    band = inner_range if inner_range > 0 else near_range
    # The honey zone (tiny-step walking band) is the wider near-range band.
    honey = max(band, near_range)
    left_honey = rope_x - honey
    right_honey = rope_x + honey
    rope_gap = rope_x - player.x
    absolute_gap = abs(rope_gap)
    minimum_confidence = float(movement_options.get("minimum_confidence", 0.55))
    if observation.confidence < minimum_confidence:
        return RopeMovementPlan(
            "detect", player, rope_x, rope_gap,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )
    tiny_min = max(0.01, float(movement_options.get("tiny_step_min_seconds", 0.05)))
    tiny_max = max(tiny_min, float(movement_options.get("tiny_step_max_seconds", 0.15)))

    if absolute_gap <= band + 1e-9:
        if not allow_climb:
            # Fresh YOLO owns the jump decision: the minimap plan must only
            # walk here, creeping toward the rope center so the character
            # enters the screen-gap jump window.  A minimap jump issued from
            # inside this band would race the YOLO jump (the two coordinate
            # systems disagree near the rope).  Each creep is a tiny random
            # step so the screen gap is re-checked every frame.
            direction = "right" if rope_gap > 1e-9 else "left"
            return RopeMovementPlan(
                "move-to-rope-edge", player, rope_x, rope_gap,
                MovementDecision(
                    direction,
                    f"inside band; tiny random step {direction} into jump range",
                    random.uniform(tiny_min, tiny_max),
                ),
            )
        if absolute_gap <= under_rope_tolerance + 1e-9:
            return RopeMovementPlan(
                "climb", player, rope_x, rope_gap,
                MovementDecision(
                    "jump_climb_up",
                    "right under rope; jump straight up",
                    float(movement_options.get("minimum_final_hold_seconds", 0.08)),
                ),
            )
        direction = "right" if rope_gap > 1e-9 else "left"
        return RopeMovementPlan(
            "climb", player, rope_x, rope_gap,
            MovementDecision(
                f"jump_climb_{direction}",
                f"inside rope band; jump {direction} inward",
                float(movement_options.get("minimum_final_hold_seconds", 0.08)),
            ),
        )
    if absolute_gap <= honey + 1e-9:
        # Inside the honey zone but outside the jump window: tiny random
        # steps toward the rope - never a big walk (overshoots the rope) and
        # never a jump (the minimap/YOLO jump gate is not satisfied yet).
        direction = "right" if rope_gap > 1e-9 else "left"
        return RopeMovementPlan(
            "move-to-rope-edge", player, rope_x, rope_gap,
            MovementDecision(
                direction,
                f"inside honey zone; tiny random step {direction} toward rope",
                random.uniform(tiny_min, tiny_max),
            ),
        )
    if player.x < left_honey - 1e-9:
        edge, direction = left_honey, "right"
    else:
        edge, direction = right_honey, "left"

    # Outside the honey zone, keep using the full fixed movement hold.
    # Shorten the hold only in the final edge-calculation zone immediately
    # before the honey-zone edge. Calculating every approach from its distance
    # made ``actual_hold`` shrink gradually across most of the platform.
    edge_gap = edge - player.x
    speed = max(0.001, float(movement_options.get("estimated_final_speed", 0.205)))
    gain = float(movement_options.get("final_move_safety_gain", 0.95))
    minimum_hold = float(movement_options.get(
        "minimum_movement_hold_seconds",
        movement_options.get("minimum_final_hold_seconds", 0.08),
    ))
    maximum_hold = float(movement_options.get("movement_hold_seconds", 2.0))
    edge_calculation_distance = max(0.0, float(
        movement_options.get("final_calculation_distance", 0.04)
    ))
    if abs(edge_gap) > edge_calculation_distance + 1e-9:
        duration = maximum_hold
        hold_detail = (
            f"outside edge zone {edge_calculation_distance:.6f}; "
            f"fixed hold {duration:.3f}s"
        )
    else:
        duration = float(np.clip(
            abs(edge_gap) / speed * gain, minimum_hold, maximum_hold
        ))
        hold_detail = (
            f"inside edge zone {edge_calculation_distance:.6f}; "
            f"calculated hold {duration:.3f}s"
        )
    return RopeMovementPlan(
        "move-to-rope-edge", player, edge, edge_gap,
        MovementDecision(
            direction,
            f"move into rope band at {edge:.6f}; {hold_detail}",
            duration,
        ),
    )


@dataclass
class ClimbState:
    """State kept between fresh screenshots while finding the rope grab point."""

    phase: str = "idle"
    baseline_y: Optional[float] = None
    baseline_world_y: Optional[float] = None
    failed_shift_used: bool = False
    # Sideways climb-jump attempts already made on this approach to reach a
    # rope whose bottom a straight jump cannot reach (stairs/benches beside
    # the rope).  The side is chosen by the worker and alternates every
    # failed cycle; this counter is informational (log + alternation within
    # one continuous approach).
    lateral_hop_cycles: int = 0
    up_held: bool = False
    progress_check_frames: int = 0
    attach_frames: int = 0
    recent_y: list[float] = field(default_factory=list)
    target_layer_frames: int = 0
    target_layer_since: Optional[float] = None
    last_world_y: Optional[float] = None
    # Last raw marker Y used by the attachment verifier.  Attachment needs
    # two *new* upward advances, not two reads of one completed jump arc.
    last_marker_y: Optional[float] = None
    stalled_frames: int = 0
    # Consecutive frames the marker sits inside the NEXT layer's arrival
    # band while holding Up (rope-top settle).  Bounds how long the
    # at-arrival stall suppression may hold Up before retrying.
    arrival_frames: int = 0
    # A return climb can miss a rope and land on a lower recorded floor.
    # Keep a short confirmation streak so one noisy layer read does not
    # redirect the rope target, but never leave the return state latched to
    # the floor the character has already fallen away from.
    return_descent_floor: Optional[str] = None
    return_descent_frames: int = 0
    # Consecutive frames with NO recorded floor detected while a climb owns Up.
    # Those frames are the character's own jump arc just above the platform row
    # (13:58 log: marker 0.617857 / world 2.71 while layer2's row is
    # 0.653571 / 3.546), not evidence against the arrival - so they must not
    # zero the arrival confirmation.  A long run of them still restarts the
    # climb, which is what this counter bounds.
    blank_arrival_frames: int = 0


def preserve_persistent_climb(
    state: ClimbState,
    proposed: MovementDecision,
) -> MovementDecision:
    """Never let a horizontal recalculation cancel an attached rope climb.

    While the climb state machine owns the Up key (``up_held``), a walk
    decision must not release it mid-grab/mid-climb - the character would
    fall off the rope.  Walk proposals are deferred; climb/jump proposals
    pass through so the state machine keeps advancing (verification,
    retries, stall detection).
    """

    if not state.up_held:
        return proposed
    # A recorded point reached while climbing is an explicit Alt+direction
    # request.  It must win over the ordinary "keep Up held" decision; the
    # jump-point executor preserves Up itself through its landing check.
    if isinstance(proposed.key, str) and proposed.key.startswith("jump_point_"):
        return proposed
    if proposed.key in ("jump_climb_left", "jump_climb_right", "jump_climb_up"):
        # The route planner recomputes from every screenshot and can suggest
        # the opposite rope side while the previous Alt+Up/Alt+side attempt
        # is still being verified.  That suggestion is not a fresh recovery
        # decision.  Keep advancing the existing state machine instead; it
        # alone may issue a new Alt chord after it has confirmed a failed
        # grab.  This also keeps the action log honest (``climb`` rather than
        # a misleading immediate ``jump_climb_left/right``).
        return MovementDecision(
            "climb", "Up held; verifying the current rope-climb attempt"
        )
    if proposed.key in ("left", "right"):
        return MovementDecision(
            None,
            "Up remains held; horizontal walk deferred until climb resolves",
        )
    if state.phase == "climbing-up":
        return MovementDecision(
            None,
            "Up remains held until the next recorded layer is confirmed",
        )
    return proposed


def _pressed(sender: Any, decision: MovementDecision) -> bool:
    return _send_tap(sender, decision)


def _directional_jump_climb(
    sender: Any,
    direction: str,
    direction_hold: float,
    climb_duration: float,
    persistent_up: bool = False,
) -> bool:
    """Press Alt+direction, then hand off immediately to Up for the grab.

    Left/Right/Up/Down are centrally serialized by ``WindowKeySender``.  A
    lateral rope jump therefore holds Alt+Left/Right for its configured jump
    window, releases the lateral key, and sends Up with no deliberate sleep
    between those two transitions.  This avoids both opposing directional
    keys being down and the former visible lag before the rope grab.
    """

    if not _sender_is_safe(sender):
        LOG.warning("directional climb suppressed: target window is not safely selected")
        return False
    key_down = getattr(sender, "key_down", None)
    key_up = getattr(sender, "key_up", None)
    press = getattr(sender, "press", None)
    if key_down is None or key_up is None or press is None:
        raise TypeError("directional climb requires key_down(), key_up(), and press()")

    LOG.info("CLIMB recovery: press Alt+%s together, then hold Up", direction)
    direction_claimed = False
    alt_claimed = False
    up_claimed = False
    failed = True
    try:
        # Alt is independent.  Directional keys are mutually exclusive, so a
        # sideways chord never overlaps its Left/Right with the following Up.
        direction_claimed = key_down(direction) is not False
        if not direction_claimed:
            return False
        alt_claimed = key_down("alt") is not False
        if not alt_claimed:
            return False
        time.sleep(max(0.025, direction_hold))
        failed = False
    finally:
        if alt_claimed:
            key_up("alt")
        # The order is intentional: lateral release immediately followed by
        # Up creates a smooth handoff without ever holding both directions.
        if direction_claimed and direction != "up":
            key_up(direction)
        if direction_claimed and direction == "up" and failed:
            key_up("up")
    # Straight-up retains the Alt+Up chord.  Persistent climbs leave Up down;
    # timed callers release it before issuing their duration-limited Up press.
    if direction == "up":
        if persistent_up:
            up_claimed = True
            up_ok = True
        else:
            key_up("up")
            up_ok = press("up", duration=climb_duration)
        return up_ok is not False
    # Sideways chord: no intentional gap after releasing Left/Right.  Up is
    # the very next input event, so a rope touched during the jump is grabbed
    # without reintroducing conflicting directional holds.
    if persistent_up:
        up_claimed = key_down("up") is not False
        return up_claimed
    return press("up", duration=climb_duration) is not False


def _drop_through_platform(
    sender: Any,
    chord_hold_seconds: float = 0.10,
) -> bool:
    """Press Alt+Down as one simultaneous chord to descend a platform."""

    if not _sender_is_safe(sender):
        LOG.warning("drop suppressed: target window is not safely selected")
        return False
    key_down = getattr(sender, "key_down", None)
    key_up = getattr(sender, "key_up", None)
    if key_down is None or key_up is None:
        raise TypeError("drop action requires key_down() and key_up()")
    down_claimed = False
    alt_claimed = False
    try:
        down_claimed = key_down("down") is not False
        if not down_claimed:
            return False
        alt_claimed = key_down("alt") is not False
        if not alt_claimed:
            return False
        time.sleep(max(0.025, chord_hold_seconds))
        return True
    finally:
        if alt_claimed:
            key_up("alt")
        if down_claimed:
            key_up("down")


def climb(
    sender: Any,
    observation: MinimapObservation,
    state: ClimbState,
    *,
    climb_duration: float = 0.45,
    nudge_duration: float = 0.10,
    y_change_required: float = 0.015,
    world_y_change_required: float = 0.75,
    world_y_stall_change_required: float = 0.15,
    world_y_stall_frames: int = 3,
    action_lock: Optional[threading.Lock] = None,
    preferred_direction: Optional[str] = None,
    failed_cycle_right_seconds: float = 0.01,
    persistent_up: bool = False,
    rope_x: Optional[float] = None,
    rope_x_tolerance: float = 0.025,
    straight_up_tolerance: float = 0.008,
    climb_attach_frames: int = 3,
    arrival_y: Optional[float] = None,
    arrival_tolerance: float = 0.02,
    arrival_in_progress: bool = False,
    # Side for the under-rope lateral recovery (see the failed-cycle block):
    # the worker alternates it after every sideways climb jump.  Left when
    # unset (direct/legacy callers).
    lateral_hop_side: Optional[str] = None,
) -> str:
    """Try to grab the rope and verify it from the next minimap screenshot.

    The first attempt is the jump appropriate to the character's position:
    a straight Alt+Up jump when directly under the rope (preferred_direction
    ``"up"``), otherwise a simultaneous directional jump toward the rope.
    Screenshots between attempts verify upward Y; failure then tries the
    opposite direction.

    "Attached" is verified from the MINIMAP, never from Y alone: the yellow
    diamond's X must be close to the rope X (``rope_x`` within
    ``rope_x_tolerance``) AND it must rise (upward Y) for
    ``climb_attach_frames`` consecutive frames.  A Y-only check falsely
    attached while the character stood beside the rope (world-Y tracker
    noise) and froze it holding Up on the ground.

    ``arrival_y``/``arrival_tolerance`` carry the NEXT layer's marker Y: when
    the marker settles within that band the character reached the platform
    (not a failed grab), so the fell-back release is suppressed and the
    layer arrival (handled by the route resync) completes the climb.

    ``straight_up_tolerance`` is deliberately narrower than the attachment
    tolerance. Only that center zone may replace a planned left/right jump
    with Alt+Up; attachment verification can remain wider without destroying
    the directional rope approach.
    """

    player = observation.player
    if player is None:
        return "waiting-marker"
    if state.phase == "succeeded":
        return "succeeded"

    def perform(decisions: list[MovementDecision]) -> bool:
        def send_all() -> bool:
            return all(_pressed(sender, decision) for decision in decisions)
        if action_lock is None:
            return send_all()
        with action_lock:
            return send_all()

    if state.phase == "idle":
        inside_straight_up_zone = bool(
            rope_x is not None
            and abs(rope_x - player.x) <= straight_up_tolerance
        )
        # Only the narrow center zone jumps vertically. The wider attachment
        # tolerance must not override a left/right jump chosen by the rope
        # planner (observed gap +0.020 incorrectly becoming Alt+Up).
        if inside_straight_up_zone:
            direction = "up"
        elif rope_x is not None:
            # A monster hit can move the character after the route planner
            # chose its earlier side.  Prefer the current rope gap so every
            # fresh climb jump heads toward the rope.
            direction = "right" if rope_x - player.x > 0 else "left"
        else:
            direction = (
                preferred_direction
                if preferred_direction in ("left", "right", "up") else "left"
            )
        def jump_toward() -> bool:
            return _directional_jump_climb(
                sender, direction, nudge_duration, climb_duration, persistent_up
            )
        ok = jump_toward() if action_lock is None else False
        if action_lock is not None:
            with action_lock:
                ok = jump_toward()
        next_phase = f"check-primary-{direction}"
        result = f"{direction}-toward-rope"
        if ok:
            state.baseline_y = player.y
            state.baseline_world_y = (
                observation.world_y_diamonds
                if observation.structure_confidence >= 0.12 else None
            )
            state.phase = next_phase
            state.up_held = persistent_up
            state.progress_check_frames = 0
            state.attach_frames = 0
            state.recent_y = []
            state.last_world_y = state.baseline_world_y
            state.last_marker_y = state.baseline_y
            state.stalled_frames = 0
            return result
        return "input-blocked"

    baseline = state.baseline_y
    if (not persistent_up and baseline is not None
            and baseline - player.y >= y_change_required):
        state.phase = "succeeded"
        LOG.info("CLIMB verified: minimap Y changed %.4f -> %.4f", baseline, player.y)
        return "succeeded"

    if state.phase == "check-initial":
        def jump_right() -> bool:
            return _directional_jump_climb(
                sender, "right", nudge_duration, climb_duration
            )
        ok = jump_right() if action_lock is None else False
        if action_lock is not None:
            with action_lock:
                ok = jump_right()
        if ok:
            state.baseline_y = player.y
            state.phase = "check-right"
            return "right-retry"
        return "input-blocked"

    # 4-frame marker-Y window for the ON-ROPE check: if the marker falls
    # back from its recent peak (Y increases again), the grab failed and the
    # character is NOT on the rope - never confirm/keep "climbing" then.
    # The raw marker Y is NOT trusted alone: during a genuine climb the
    # minimap can scroll and the marker Y jumps while the world Y keeps
    # advancing.  Only treat it as a failed grab when the world Y is NOT
    # advancing.
    marker_frame_progress: Optional[float] = None
    if persistent_up and state.up_held:
        if observation.player is not None:
            if state.last_marker_y is not None:
                marker_frame_progress = state.last_marker_y - player.y
            state.last_marker_y = player.y
            state.recent_y.append(player.y)
            if len(state.recent_y) > 4:
                state.recent_y.pop(0)
        world_advancing = bool(
            state.baseline_world_y is not None
            and observation.world_y_diamonds is not None
            and observation.structure_confidence >= 0.12
            and (state.baseline_world_y - observation.world_y_diamonds)
            >= world_y_change_required
        )
        # The marker settled within the NEXT layer's band: the character
        # reached the platform (the rope top settle is not a failed grab).
        # The layer arrival completes the climb instead of releasing Up.
        at_arrival = bool(
            arrival_y is not None
            and observation.player is not None
            and abs(player.y - arrival_y) <= arrival_tolerance
        )
        fell_back = bool(
            not world_advancing
            and not at_arrival
            and len(state.recent_y) >= 2
            and observation.player is not None
            and player.y >= min(state.recent_y) + y_change_required
        )
    else:
        fell_back = False

    if persistent_up and state.up_held and state.phase == "climbing-up":
        if at_arrival or arrival_in_progress:
            # Reached the rope top (either the marker settled in the next
            # layer's band, or the worker's layer-confirmation is already
            # counting): the world-Y tracker re-anchors and the screen Y is
            # at its minimum there, so "no Y progress" is the EXPECTED state
            # - not a stalled grab.  Keep Up held so the arrival confirmation
            # completes and the character steps onto the platform; releasing
            # Up here made it fall back off the rope top (observed: 'CLIMB
            # stalled' right at layer arrival).
            state.arrival_frames += 1
            if state.arrival_frames <= 8:
                # ~0.8s at 10fps: the frame-based arrival confirmation
                # (climb_layer_confirm_frames frames, default 3) finishes well
                # inside this bound.  Up is released the moment the worker
                # confirms the next layer - there is no timed compensation.
                state.stalled_frames = 0
                state.last_world_y = observation.world_y_diamonds
                return "climbing-up"
            state.arrival_frames = 0
        else:
            state.arrival_frames = 0
        if fell_back:
            # The marker descended from its jump peak: the grab failed and
            # the character fell back.  Release Up immediately and restart
            # the recovery - holding Up here froze the character under the
            # rope after a failed jump.
            key_up = getattr(sender, "key_up", None)
            if key_up is not None:
                key_up("up")
            state.phase = "idle"
            state.baseline_y = None
            state.baseline_world_y = None
            state.up_held = False
            state.progress_check_frames = 0
            state.attach_frames = 0
            state.last_world_y = None
            state.stalled_frames = 0
            state.recent_y = []
            LOG.warning("CLIMB grab failed: marker fell back; restarting recovery")
            return "climb-stalled-retry"
        if (observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            if state.last_world_y is not None:
                frame_progress = state.last_world_y - observation.world_y_diamonds
                if frame_progress >= world_y_stall_change_required:
                    state.stalled_frames = 0
                else:
                    state.stalled_frames += 1
            state.last_world_y = observation.world_y_diamonds
            if state.stalled_frames >= max(1, int(world_y_stall_frames)):
                key_up = getattr(sender, "key_up", None)
                if key_up is not None:
                    key_up("up")
                state.phase = "idle"
                state.baseline_y = None
                state.baseline_world_y = None
                state.up_held = False
                state.progress_check_frames = 0
                state.last_world_y = None
                state.stalled_frames = 0
                LOG.warning(
                    "CLIMB stalled: world Y stopped advancing; restarting rope recovery"
                )
                return "climb-stalled-retry"
        else:
            # No reliable world-Y reference: fall back to screen Y.  The
            # attach check can fire mid-jump-arc (the marker rises then falls
            # back on a failed grab); without this stall the Up key stays
            # held forever and the character never jumps again.
            if baseline is None or baseline - player.y >= y_change_required:
                state.stalled_frames = 0
            else:
                state.stalled_frames += 1
                if state.stalled_frames >= max(1, int(world_y_stall_frames)):
                    key_up = getattr(sender, "key_up", None)
                    if key_up is not None:
                        key_up("up")
                    state.phase = "idle"
                    state.baseline_y = None
                    state.baseline_world_y = None
                    state.up_held = False
                    state.progress_check_frames = 0
                    state.last_world_y = None
                    state.stalled_frames = 0
                    LOG.warning(
                        "CLIMB stalled: screen Y stopped advancing; "
                        "restarting rope recovery"
                    )
                    return "climb-stalled-retry"
        return "climbing-up"

    if persistent_up and state.up_held:
        state.progress_check_frames += 1
        # ATTACH = minimap marker horizontally aligned with the rope AND
        # rising (upward Y) for climb_attach_frames consecutive frames.
        # Y-only checks falsely "attached" while the character stood beside
        # the rope (world-Y tracker noise) and froze it holding Up.
        if rope_x is not None and observation.player is not None:
            x_gap = abs(observation.player.x - rope_x)
            # Once the marker has supplied an independent upward confirmation
            # at the rope, minimap scrolling/diamond quantisation can move its
            # displayed X by another pixel while it climbs.  Keep that rising
            # session attached with one marker-row of lateral grace; otherwise
            # the third confirmation resets at the rope lip and the planner
            # proposes an unnecessary opposite ``jump_climb_left/right``.
            # Before the first rise the strict tolerance remains in force, so
            # a character merely standing beside a rope is never attached.
            attached_x_tolerance = rope_x_tolerance
            if state.attach_frames:
                attached_x_tolerance += 0.008
            x_aligned = x_gap <= attached_x_tolerance
        else:
            x_gap = None
            x_aligned = True
        # The old verifier re-used the same large rise from the initial jump
        # on every frame.  At the jump apex that made two unchanged frames
        # look like two confirmations, falsely marking the character as on
        # the rope and holding Up forever.  Count only a NEW marker/world
        # advance; a real rope climb produces at least two such advances.
        marker_rising = bool(
            marker_frame_progress is not None
            and marker_frame_progress >= max(0.003, y_change_required * 0.35)
        )
        world_frame_progress = 0.0
        world_rising = False
        if (state.baseline_world_y is not None
                and observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            world_progress = (
                state.baseline_world_y - observation.world_y_diamonds
            )
            if state.last_world_y is not None:
                world_frame_progress = (
                    state.last_world_y - observation.world_y_diamonds
                )
                world_rising = (
                    world_frame_progress >= world_y_stall_change_required
                )
            state.last_world_y = observation.world_y_diamonds
            progress_detail = f"world Y +{world_progress:.3f} diamonds"
        else:
            world_progress = 0.0
            progress_detail = (
                f"screen Y +{baseline - player.y:.6f}"
                if baseline is not None else "screen Y n/a"
            )
        if x_aligned and (marker_rising or world_rising) and not fell_back:
            state.attach_frames += 1
            if state.attach_frames >= max(1, int(climb_attach_frames)):
                state.phase = "climbing-up"
                state.last_world_y = observation.world_y_diamonds
                state.stalled_frames = 0
                LOG.info(
                    "CLIMB attached: keeping Up held (x_gap=%s %s)",
                    f"{x_gap:.4f}" if x_gap is not None else "n/a",
                    progress_detail,
                )
                return "climbing-up"
            # First independent confirmation: keep Up held and wait for a
            # second *new* advance.  Repeated identical observations do not
            # count as a rope grab.
            return "holding-up-awaiting-progress"
        if not x_aligned or fell_back:
            state.attach_frames = 0
        elif state.attach_frames:
            # The first upward movement may simply be the apex of an ordinary
            # jump. Give delayed minimap/world updates a short grace window,
            # but never promote an unchanged frame to "attached".
            if state.progress_check_frames < 6:
                return "holding-up-awaiting-progress"
            state.attach_frames = 0
        # Phase-correlation and the game animation can lag the jump chord by
        # several minimap frames. Keep Up owned during that grace period;
        # releasing it on the first centered-diamond frame makes the character
        # jump away from the rope before the map starts scrolling.
        if state.progress_check_frames < 4:
            return "holding-up-awaiting-progress"
        # No upward progress on the fresh screenshot: release this Up claim
        # before another directional jump attempt.
        key_up = getattr(sender, "key_up", None)
        if key_up is not None:
            key_up("up")
        state.up_held = False

    if state.phase == "climbing-up":
        return "climbing-up"

    if state.phase in (
        "check-right", "check-primary-right", "check-primary-left",
        "check-primary-up",
    ):
        # Recalculate from the newest screenshot. Never blindly reverse the
        # prior jump: if the character remains left of the rope, retry Right;
        # if it is now right of the rope, retry Left.  A failed straight-up
        # jump retries toward the rope SIDE the character is actually on
        # (the live minimap gap) instead of a blind "right" that shoves the
        # character past the rope.
        inside_straight_up_zone = bool(
            rope_x is not None
            and abs(rope_x - player.x) <= straight_up_tolerance
        )
        skip_straight_retry = bool(
            inside_straight_up_zone
            and state.phase == "check-primary-up"
        )
        if skip_straight_retry:
            # Directly under the rope gets one Alt+Up attempt only.  If it
            # fails, fall through immediately to the sideways early-Up hop
            # below; a second identical jump only added delay on high ropes.
            LOG.info(
                "CLIMB under-rope primary Alt+Up failed; trying sideways recovery"
            )
            retry_direction = None
        elif inside_straight_up_zone:
            retry_direction = "up"
        elif player is not None and rope_x is not None:
            retry_direction = "right" if rope_x - player.x > 0 else "left"
        elif preferred_direction in ("left", "right"):
            retry_direction = preferred_direction
        else:
            retry_direction = (
                "left" if state.phase in ("check-right", "check-primary-right")
                else "right"
            )

        if retry_direction is not None:
            def jump_toward_current_rope_side() -> bool:
                return _directional_jump_climb(
                    sender, retry_direction, nudge_duration, climb_duration, persistent_up
                )
            ok = jump_toward_current_rope_side() if action_lock is None else False
            if action_lock is not None:
                with action_lock:
                    ok = jump_toward_current_rope_side()
            if ok:
                state.baseline_y = player.y
                state.baseline_world_y = (
                    observation.world_y_diamonds
                    if observation.structure_confidence >= 0.12 else None
                )
                state.phase = "check-opposite"
                state.up_held = persistent_up
                state.progress_check_frames = 0
                state.attach_frames = 0
                state.recent_y = []
                state.last_world_y = state.baseline_world_y
                state.last_marker_y = state.baseline_y
                state.stalled_frames = 0
                return f"{retry_direction}-retry-toward-rope"
            return "input-blocked"

    # Both attempts failed.  Recovery depends on where the character is:
    #
    # - RIGHT UNDER THE ROPE (straight-up zone): the one plain jump+climb
    #   attempt failed, so this rope bottom hangs above jump reach
    #   here (or a knock-down left the character on a step below it).  Try a
    #   real SIDEWAYS climb jump - Alt+Left / Alt+Right with Up held from the
    #   very start of the jump (the worker alternates the side every failed
    #   cycle): a stair/bench beside the rope may be reachable and the rope
    #   grabbable from the higher spot, and the early Up also grabs the rope
    #   the moment the sideways jump touches it.
    # - ELSEWHERE: keep the legacy one-time correction toward the rope.
    under_rope = bool(
        rope_x is not None
        and player is not None
        and abs(rope_x - player.x) <= straight_up_tolerance
    )
    if under_rope:
        # Always jump back TOWARD the live rope location.  When the marker is
        # exactly centred (or quantised to the same minimap pixel), keep the
        # alternating fallback so consecutive attempts explore both sides.
        rope_gap = rope_x - player.x
        if abs(rope_gap) > 1e-9:
            side = "right" if rope_gap > 0 else "left"
        else:
            side = (
                lateral_hop_side
                if lateral_hop_side in ("left", "right") else "left"
            )
        pre_step_direction = "right" if side == "left" else "left"
        state.lateral_hop_cycles += 1

        def climb_jump_side() -> bool:
            # Move a small step AWAY first, then jump back toward the rope.
            # This gives the sideways jump a useful launch arc instead of
            # repeatedly jumping from the exact same under-rope pixel.
            press = getattr(sender, "press", None)
            if not callable(press):
                raise TypeError("lateral climb recovery requires press()")
            LOG.info(
                "CLIMB under-rope recovery: small step %s, then Alt+%s with early Up",
                pre_step_direction, side,
            )
            if press(pre_step_direction, duration=nudge_duration) is False:
                return False
            return _directional_jump_climb(
                sender, side, nudge_duration, climb_duration, persistent_up
            )
        ok = climb_jump_side() if action_lock is None else False
        if action_lock is not None:
            with action_lock:
                ok = climb_jump_side()
        if ok:
            state.baseline_y = player.y
            state.baseline_world_y = (
                observation.world_y_diamonds
                if observation.structure_confidence >= 0.12 else None
            )
            state.phase = "check-opposite"
            state.up_held = persistent_up
            state.progress_check_frames = 0
            state.attach_frames = 0
            state.recent_y = []
            state.last_world_y = state.baseline_world_y
            state.last_marker_y = state.baseline_y
            state.stalled_frames = 0
            LOG.warning(
                "CLIMB under-rope primary jump failed; step %s then sideways "
                "climb jump %s with early Up (lateral #%d)",
                pre_step_direction, side, state.lateral_hop_cycles,
            )
            return f"{side}-lateral-toward-rope"
        state.phase = "idle"
        state.baseline_y = None
        state.baseline_world_y = None
        state.up_held = False
        state.progress_check_frames = 0
        state.last_world_y = None
        state.stalled_frames = 0
        state.recent_y = []
        LOG.warning("CLIMB under-rope sideways climb jump %s was blocked", side)
        return "input-blocked"
    if state.failed_shift_used:
        state.phase = "idle"
        state.baseline_y = None
        state.baseline_world_y = None
        state.up_held = False
        state.progress_check_frames = 0
        state.last_world_y = None
        state.stalled_frames = 0
        LOG.warning("CLIMB failed again; rope correction already used for this approach")
        return "failed-cycle-no-more-shift"
    correction_direction = (
        "right"
        if rope_x is None or rope_x - player.x >= 0
        else "left"
    )
    shifted = perform([
        MovementDecision(
            correction_direction,
            "one-time correction toward rope after failed climb cycle",
            failed_cycle_right_seconds,
        )
    ])
    state.phase = "idle"
    state.baseline_y = None
    state.baseline_world_y = None
    state.up_held = False
    state.progress_check_frames = 0
    state.last_world_y = None
    state.stalled_frames = 0
    if shifted:
        state.failed_shift_used = True
        LOG.warning(
            "CLIMB not verified; shifted %s toward rope %.3fs",
            correction_direction, failed_cycle_right_seconds,
        )
        return "failed-cycle-shifted-right"
    LOG.warning("CLIMB not verified and rope correction was blocked")
    return "input-blocked"


# Broad top-left crop in ABSOLUTE client pixels (the HUD is fixed pixel;
# only the viewport scales).  Only the map drawing inside the top-left
# minimap panel.  The old broad crop included yellow monsters/items in the
# game world and could mistake those for the player diamond.
DEFAULT_MINIMAP_REGION = (0, 0, 400, 400)

# 卡住判定阈值：标记 X 变化 < 0.012（最小地图单位）即视为"没在动"。
# 按帧判定（连续 10 帧 ≈ 2.5s）触发跳跃，避免把攻击动作的短暂停顿误判为台阶。
STAIR_JUMP_STALL_FALLBACK = 0.012




def _image_from_frame(frame: Any) -> Image.Image:
    """Accept a PIL image, numpy image, or common capture-frame wrappers."""

    candidate = frame
    if isinstance(frame, tuple) and frame:
        # Capture workers often publish (timestamp, image).
        candidate = next((v for v in reversed(frame) if isinstance(v, (Image.Image, np.ndarray))), frame[-1])
    for attr in ("image", "screenshot", "frame"):
        if hasattr(candidate, attr):
            candidate = getattr(candidate, attr)
            break
    if isinstance(candidate, Image.Image):
        return candidate.convert("RGB")
    if isinstance(candidate, np.ndarray):
        array = candidate
        if array.ndim == 3 and array.shape[2] == 4:
            array = array[:, :, :3]
        return Image.fromarray(array.astype(np.uint8), mode="RGB")
    raise TypeError(f"unsupported frame type: {type(frame)!r}")


def _crop(image: Image.Image, region: tuple[float, float, float, float]) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    width, height = image.size
    x0, y0, x1, y1 = region
    # Movement normally receives the detector's per-frame NORMALIZED analysis
    # box (all values in 0..1); the static DEFAULT_MINIMAP_REGION fallback is
    # ABSOLUTE pixels (values > 1) because the HUD is fixed pixel.  Handle
    # both so the fixed-pixel minimap works at any window size.
    if all(-0.01 <= value <= 1.01 for value in region):
        box = (int(x0 * width), int(y0 * height), int(x1 * width), int(y1 * height))
    else:
        box = (int(x0), int(y0), int(x1), int(y1))
    return np.asarray(image.crop(box), dtype=np.uint8), box


def detect_marker(minimap_rgb: np.ndarray) -> tuple[Optional[Point], float]:
    """Locate a saturated yellow diamond/arrow in a minimap RGB image."""
    detection = detect_yellow_diamond(minimap_rgb)
    if detection is None:
        return None, 0.0
    return Point(detection.x, detection.y), detection.confidence


def _find_upper_connector(minimap_rgb: np.ndarray, player: Point) -> Optional[Point]:
    """Infer a ladder/rope as a thin vertical map feature above the player.

    Maple minimaps vary in palette, so this uses brightness/chroma contrast
    rather than a single line color.  Ambiguous frames return ``None`` and the
    worker safely waits instead of wandering.
    """

    rgb = minimap_rgb.astype(np.int16)
    high = rgb.max(axis=2)
    low = rgb.min(axis=2)
    visible = (high >= 115) & ((high - low >= 28) | (high >= 205))
    # Remove yellow player pixels from geometry.
    visible &= ~((rgb[:, :, 0] >= 175) & (rgb[:, :, 1] >= 145) & (rgb[:, :, 2] <= 150))
    height, width = visible.shape
    px, py = player.x * width, player.y * height
    best: tuple[float, Point] | None = None
    # A connector produces several visible pixels in a narrow column and must
    # reach materially above the player's current level.
    for x in range(1, width - 1):
        column = visible[:, max(0, x - 1) : min(width, x + 2)].any(axis=1)
        runs: list[tuple[int, int]] = []
        start: Optional[int] = None
        for y, on in enumerate(column):
            if on and start is None:
                start = y
            elif not on and start is not None:
                runs.append((start, y - 1))
                start = None
        if start is not None:
            runs.append((start, height - 1))
        for top, bottom in runs:
            run = bottom - top + 1
            if run < max(7, int(height * 0.055)) or top >= py - height * 0.035:
                continue
            # The lower endpoint should be on/near the current level.  Permit
            # generous error because the marker may float above a platform.
            vertical_gap = abs(bottom - py)
            if vertical_gap > height * 0.20:
                continue
            distance = abs(x - px) + vertical_gap * 0.35
            target = Point(x / width, min(1.0, bottom / height))
            if best is None or distance < best[0]:
                best = (distance, target)
    return best[1] if best else None


def _find_current_platform(minimap_rgb: np.ndarray, player: Point) -> Optional[float]:
    """Estimate the current platform's normalized y level near the marker."""

    rgb = minimap_rgb.astype(np.int16)
    high, low = rgb.max(axis=2), rgb.min(axis=2)
    visible = (high >= 110) & ((high - low >= 24) | (high >= 200))
    visible &= ~((rgb[:, :, 0] >= 175) & (rgb[:, :, 1] >= 145) & (rgb[:, :, 2] <= 150))
    height, width = visible.shape
    px, py = int(player.x * width), int(player.y * height)
    radius = max(8, int(width * 0.08))
    left, right = max(0, px - radius), min(width, px + radius + 1)
    best: tuple[float, int] | None = None
    for y in range(max(0, py - int(height * 0.08)), min(height, py + int(height * 0.14) + 1)):
        row = visible[y, left:right]
        count = int(row.sum())
        if count < max(4, int((right - left) * 0.18)):
            continue
        score = count - abs(y - py) * 0.25
        if best is None or score > best[0]:
            best = (score, y)
    return best[1] / height if best else None


def analyze_minimap(
    frame: Any,
    region: tuple[float, float, float, float] = DEFAULT_MINIMAP_REGION,
) -> MinimapObservation:
    image = _image_from_frame(frame)
    minimap, box = _crop(image, region)
    marker = detect_yellow_diamond(minimap)
    player = Point(marker.x, marker.y) if marker is not None else None
    confidence = marker.confidence if marker is not None else 0.0
    target = _find_upper_connector(minimap, player) if player is not None else None
    platform_y = _find_current_platform(minimap, player) if player is not None else None
    action = "climb" if target is not None else "unknown"
    return MinimapObservation(
        player=player,
        target=target,
        confidence=confidence,
        minimap_box=box,
        platform_y=platform_y,
        action=action,
        marker_pixel_size=marker.pixel_size if marker is not None else None,
        analysis_size=(minimap.shape[1], minimap.shape[0]),
    )


def plan_movement(
    observation: MinimapObservation,
    horizontal_tolerance: float = 0.010,
    minimum_confidence: float = 0.55,
    fixed_target_x: Optional[float] = None,
    movement_hold_seconds: float = 2.0,
    minimum_final_hold_seconds: float = 0.08,
    estimated_minimap_speed: float = 0.11,
    final_calculation_distance: float = 0.04,
    estimated_final_speed: float = 0.205,
    final_move_safety_gain: float = 0.95,
    jump_when_near: bool = True,
    under_rope_tolerance: float = 0.008,
) -> MovementDecision:
    if observation.player is None or observation.confidence < minimum_confidence:
        return MovementDecision(None, "yellow marker missing or uncertain")
    if fixed_target_x is not None:
        target_x = fixed_target_x
    elif observation.target is not None:
        target_x = observation.target.x
    else:
        return MovementDecision(None, "no reliable upper-layer connector found")
    delta_x = target_x - observation.player.x
    comparison_epsilon = 1e-9
    if abs(delta_x) <= horizontal_tolerance + comparison_epsilon:
        if jump_when_near:
            # Right under the rope (within the tiny under-rope band) the
            # character jumps straight up - a left/right chord from directly
            # under the rope shoves it past the rope.  Slightly off-center it
            # still jumps toward the rope side.
            if abs(delta_x) <= under_rope_tolerance + comparison_epsilon:
                return MovementDecision(
                    "jump_climb_up",
                    "right under rope; jump straight up",
                    minimum_final_hold_seconds,
                )
            direction = "right" if delta_x > 0 else "left"
            return MovementDecision(
                f"jump_climb_{direction}",
                f"within rope tolerance; jump {direction} toward rope",
                minimum_final_hold_seconds,
            )
        return MovementDecision("aligned", "important endpoint reached")
    distance = abs(delta_x)
    # Use fixed-size walking only while outside the near-rope zone. Once near,
    # jump toward the rope instead of issuing tiny walking corrections.
    remaining = max(0.0, distance - horizontal_tolerance)
    if distance > final_calculation_distance + comparison_epsilon:
        duration = movement_hold_seconds
        detail = (f"distance={distance:.3f} outside final-zone="
                  f"{final_calculation_distance:.3f}; fixed_hold={duration:.3f}s")
    else:
        if not jump_when_near:
            # Important patrol positions are crossing lines, not precision
            # stops. Keep the normal walking hold all the way through them;
            # the route state machine advances as soon as the marker crosses
            # the saved X, so there is no slow/tiny-step phase.
            duration = movement_hold_seconds
            detail = (f"distance={distance:.3f} near route endpoint; "
                      f"fixed_hold={duration:.3f}s (no tiny correction)")
            if delta_x < 0:
                return MovementDecision("left", detail, duration)
            return MovementDecision("right", detail, duration)
        direction = "left" if delta_x < 0 else "right"
        return MovementDecision(
            f"jump_climb_{direction}",
            f"distance={distance:.3f} inside near-rope zone; jump {direction} toward rope",
            minimum_final_hold_seconds,
        )
    if delta_x < 0:
        return MovementDecision("left", f"calculated Left hold ({detail})",
                                duration)
    return MovementDecision("right", f"calculated Right hold ({detail})",
                            duration)


def _sender_is_safe(sender: Any) -> bool:
    """Honor optional focus checks exposed by different sender versions."""

    for name in ("is_target_focused", "is_window_focused", "is_focused"):
        checker = getattr(sender, name, None)
        if checker is not None:
            try:
                return bool(checker())
            except Exception:
                LOG.exception("window focus check failed")
                return False
    # A correctly scoped WindowKeySender posts to a known HWND, not the global
    # foreground window.  Unknown senders must explicitly opt in as safe.
    configured = bool(
        getattr(sender, "targets_configured_window", False)
        or getattr(sender, "window_title", None)
        or getattr(sender, "hwnd", None)
        or sender.__class__.__name__ == "WindowKeySender"
    )
    return configured or bool(getattr(sender, "dry_run", True))


def _send_tap(sender: Any, decision: MovementDecision) -> bool:
    if decision.key is None:
        return False
    if not _sender_is_safe(sender):
        LOG.warning("movement suppressed: target window is not safely selected")
        return False
    press = (
        getattr(sender, "press", None)
        or getattr(sender, "tap", None)
        or getattr(sender, "send_key", None)
        or getattr(sender, "send", None)
    )
    if press is None:
        raise TypeError("key sender must provide press(), tap(), send_key(), or send()")
    if decision.key == "climb":
        # MapleStory grabs a rope reliably by jumping first, then holding Up.
        LOG.info("CLIMB: jump Alt, then hold Up for %.3fs", decision.duration)
        try:
            alt_ok = press("alt", duration=0.025)
            time.sleep(0.06)
            up_ok = press("up", duration=decision.duration)
        except TypeError:
            alt_ok = press("alt")
            time.sleep(0.06)
            up_ok = press("up")
        success = alt_ok is not False and up_ok is not False
        if success:
            LOG.info("CLIMB complete: Alt + Up sent")
        else:
            LOG.warning("CLIMB failed: Alt or Up was blocked; will retry")
        return success
    try:
        result = press(decision.key, duration=decision.duration)
    except TypeError:
        result = press(decision.key)
    return result is not False


class MovementWorker(threading.Thread):
    """Consume only the newest frame and issue at most one short tap per frame."""

    def _next_layer_arrival_band(self) -> tuple[Optional[float], float]:
        """(marker Y, tolerance) of the floor the climb is heading to.

        The NEXT route layer is the normal answer.  A return climb is the
        exception: the patrol range may start above the floor the character is
        returning through, so the floor it is actually climbing to is an
        out-of-range intermediate one (the operator's layer2 while the range
        starts at layer3).  Asking only for the next ROUTE layer returned
        ``None`` there, so the arrival check inside the climb state machine could
        never be true: at the platform top the stall detector saw "world Y
        stopped advancing", released Up and re-jumped forever (13:58 log:
        standing on layer2 at world 3.546, looping "CLIMB stalled").

        The band is widened by one marker row on both sides: the marker lands on
        the platform row with the diamond's own 1 px quantum, and for a
        single-supporter floor that row is the whole band.
        """

        candidates: list[str] = []
        if self._return_mode == "climb-to-route":
            # A return climb heads for the recorded floor directly above the one
            # it is climbing from.  That is NOT necessarily the next route layer:
            # while the character returns through a floor below the patrol range,
            # the route index still describes the floor it fell from, so its
            # "next" layer sits one floor too high and its band never matches the
            # platform the character actually reaches.
            target = self._return_climb_target_floor()
            if target is not None:
                candidates.append(target)
        if (self._route_layer_index is not None
                and self._route_layer_index + 1 < len(self._route_layers)):
            next_route = self._route_layers[self._route_layer_index + 1]
            if next_route not in candidates:
                candidates.append(next_route)
        for name in candidates:
            layer = self.important_positions.get(name, {})
            if not _has_layer_y_supporter(layer):
                continue
            tolerance = float(layer.get("y_tolerance", 0.020000))
            band = _layer_y_band(layer, tolerance)
            if band is None:
                continue
            margin = _layer_marker_row(layer) or 0.0
            low = band[0] - margin
            high = band[1] + margin
            return (low + high) / 2.0, (high - low) / 2.0
        return None, 0.02

    def _return_climb_target_floor(self) -> Optional[str]:
        """The recorded floor a return climb is climbing toward.

        That is the next recorded floor ABOVE the floor the return is climbing
        from, capped at the bottom of the patrol range (the return target
        itself).  It is not necessarily in ``_route_layers`` - see
        ``_next_layer_arrival_band``.
        """

        departing = self._return_from_floor
        if departing is None:
            return None
        departing_number = _layer_number(departing)
        best: Optional[tuple[int, str]] = None
        for name, layer in self.important_positions.items():
            if name == departing or not isinstance(layer, dict):
                continue
            if not _has_layer_y_supporter(layer):
                continue
            number = _layer_number(name)
            if number <= departing_number or number > self._patrol_range_min:
                continue
            if best is None or number < best[0]:
                best = (number, name)
        return best[1] if best is not None else None

    def _run_climb_step(
        self,
        observation: MinimapObservation,
        route_target_x: Optional[float],
        preferred_direction: Optional[str],
    ) -> str:
        """Advance the persistent climb state machine one frame."""

        arrival_y, arrival_tolerance = self._next_layer_arrival_band()
        # 层到达确认已经开始（worker 的图层逻辑正在计数）时，到顶的停滞
        # 检测必须抑制：即使 arrival_y 缺失/未命中，也保持 Up 直到确认完成。
        arrival_in_progress = bool(
            self._climb_state.target_layer_frames > 0
        )
        # A climb chord owns all directional keys while it is emitted.  This
        # shares the same lock as 小碎步, so no left/right pair can land between
        # Alt+side and the immediately-following Up grab.
        with self._direction_lock:
            result = climb(
            self.key_sender,
            observation,
            self._climb_state,
            climb_duration=self.climb_up_hold_seconds,
            nudge_duration=self.climb_nudge_seconds,
            y_change_required=self.climb_y_change_required,
            world_y_change_required=self.climb_world_y_change_required,
            world_y_stall_change_required=(
                self.climb_world_y_stall_change_required
            ),
            world_y_stall_frames=self.climb_world_y_stall_frames,
            action_lock=self.climb_attack_lock,
            preferred_direction=preferred_direction,
            failed_cycle_right_seconds=self.climb_failed_shift_right_seconds,
            persistent_up=True,
            rope_x=route_target_x,
            straight_up_tolerance=self.under_rope_tolerance,
            arrival_y=arrival_y,
            arrival_tolerance=arrival_tolerance,
            arrival_in_progress=arrival_in_progress,
                lateral_hop_side=self._climb_lateral_side,
            )
        LOG.info("climb recovery state: %s", result)
        if result == "succeeded" and self._route_layers:
            self._climb_arrival_at = time.monotonic()
            self._climb_cycle_reset()
            # A real grab ended the under-rope lateral-recovery streak.
            self._climb_lateral_streak = 0
            if self._return_mode == "climb-to-route":
                # Return climb landed on a higher floor: re-detect where we
                # are instead of advancing the normal route.  An in-range
                # floor restarts patrol; an out-of-range floor keeps climbing
                # from the new floor's own rope.
                self._resolve_fall(observation)
            else:
                self._advance_after_climb()
        elif result == "failed-cycle-no-more-shift":
            # Both jump directions failed and the one-time correction is
            # used up: a stuck-under-the-rope character restarts the route
            # at left-most after a few cycles instead of jumping in place.
            self._climb_cycle_failed()
        elif isinstance(result, str) and result.endswith("-lateral-toward-rope"):
            # Under-rope sideways climb jump: alternate the side for the
            # next attempt and count the streak.  The walk back to the rope
            # must NOT reset this streak (walks reset the ordinary failure
            # counter), otherwise a rope the sideways jumps cannot reach
            # would loop forever without ever restarting the approach.
            self._climb_lateral_side = (
                "right" if self._climb_lateral_side == "left" else "left"
            )
            self._climb_lateral_streak += 1
            if self._climb_lateral_streak >= self.climb_lateral_cycles_reset:
                self._climb_lateral_streak = 0
                self._escalate_failed_climb_approach()
        return result

    def _rescue_stuck_check(
        self, observation: MinimapObservation, now: float
    ) -> None:
        """Self-rescue stuck detection (checked once per 5-minute window).

        Long patrols can wedge the character in a corner or on a rope with
        no position change.  Within each ``rescue_check_interval_seconds``
        window the run of consecutive unchanged minimap positions is tracked
        (2 minimap pixels tolerance absorbs marker jitter); if it ever
        reaches ``rescue_stuck_frames`` (default 25 at 5 fps) the character is stuck:
        drop to layer1 and restart the patrol.  Frames where an attack is
        active are skipped - movement is intentionally paused then.

        A MISSING yellow marker is deliberately *not* a self-rescue condition.
        It can mean a login/offline page, a minimap-less zone, or a transient
        capture failure.  The character worker owns that state and starts
        auto reconnect only after its independent login-page confirmation.
        Sending Alt+Down without a marker was unsafe: it could issue thirty
        blind drop chords while reconnect was still deciding whether the game
        was offline.

        A marker present but OFF every recorded layer band has a separate
        consecutive stationary run: the character may have fallen off, but a
        transient adaptive-band miss while X/Y is progressing must not inherit
        ordinary stuck frames and drop a valid patrol floor.  A stationary
        off-route run rescues immediately at ``rescue_stuck_frames``. Frames
        with an active climb/drop are skipped because the marker legitimately
        passes between layer bands while climbing.

        站桩攻击 is exempt entirely: it stands still on purpose, and its own
        temporary anchor owns displacement recovery.

        The planned descent to the route's first layer is exempt as well: it stands
        ON a platform between Alt+Down chords, which is not "stuck", and a rescue
        there would restart the patrol on an intermediate floor - exactly the
        mingling the operator rejected ("the back to base patrol layer should block
        the hit down by monster function, don't trigger back to patrol route").
        The descent is bounded, so it hands the machine back by itself.
        """
        if not self.patrol_enabled or not self._route_layers:
            return
        if (self.reconnect_active_event is not None
                and self.reconnect_active_event.is_set()):
            # Login/channel screens legitimately hide or freeze minimap
            # movement. A rescue queued before disconnect must not keep
            # issuing old Alt+Down actions or rewrite the fresh post-login
            # drop-to-route state.
            self._rescue_stuck_frames = 0
            self._rescue_off_route_frames = 0
            self._rescue_last_pos = None
            return
        if self._descending_to_first:
            self._rescue_stuck_frames = 0
            self._rescue_off_route_frames = 0
            self._rescue_last_pos = None
            return
        if self.stationary_attack_enabled:
            # 站桩攻击 stands still BY DESIGN: every frame is "stuck" for this
            # detector, which would probe the character (walk right then left,
            # with attacks blocked) and, when the probe cannot move it at a
            # platform edge, drop it a floor - destroying the very standing
            # spot the mode protects.  The temporary anchor owns displacement
            # recovery in this mode instead.
            return
        if now - self._rescue_last_check >= self.rescue_check_interval_seconds:
            if self._rescue_max_stuck >= self.rescue_stuck_frames:
                LOG.warning(
                    "SELF-RESCUE: character stuck (%d unchanged frames in "
                    "the window); dropping to layer1 and restarting patrol",
                    self._rescue_max_stuck,
                )
                self._trigger_rescue()
            self._rescue_stuck_frames = 0
            self._rescue_max_stuck = 0
            self._rescue_off_route_frames = 0
            self._rescue_off_route_anchor = None
            self._rescue_last_check = now
        if self._attack_state is not None and self._attack_state.is_active():
            return
        pos = observation.player
        if pos is None:
            self._rescue_off_route_frames = 0
            self._rescue_off_route_anchor = None
            self._rescue_stuck_frames = 0
            self._rescue_max_stuck = 0
            self._rescue_last_pos = None
            return
        # Off-route: the marker is present but matches NO recorded layer
        # band (marker-Y check - the pinned world-Y must not be consulted
        # here).  The character is off the patrol platform: it fell off /
        # was knocked off, or the minimap frame drifted so the normalized
        # Y no longer lands on the recorded band.  The route stays pinned
        # to the recorded layer, so the phantom marker can never cross the
        # recorded boundaries - the patrol would push into the wall
        # forever (stair-jump give-up only flips left/right).  Count it as
        # stuck and rescue once the run is long enough.  Frames with an
        # active climb/drop are skipped: the marker legitimately passes
        # between bands while climbing.  Only applies when the route
        # actually defines Y bands (recorded layers always do).
        route_layers = {
            name: self.important_positions[name]
            for name in self._route_layers
        }
        has_y_bands = any(
            _has_layer_y_supporter(layer)
            for layer in route_layers.values()
        )
        off_route = bool(
            has_y_bands
                and self._climb_state.phase == "idle"
                and not self._descending_to_first
                and self._return_mode is None
                and detect_layer_by_y(pos.y, route_layers) is None
        )
        if off_route:
            # Adaptive minimap resizing can briefly put a valid platform just
            # outside every projected band. Rescue only if that condition is
            # consecutive AND the marker remains stationary. Visible X/Y
            # progress means the patrol is still working and must not drop.
            anchor = self._rescue_off_route_anchor
            if (anchor is None
                    or abs(pos.x - anchor.x) >= 0.02
                    or abs(pos.y - anchor.y) >= 0.02):
                self._rescue_off_route_anchor = Point(pos.x, pos.y)
                self._rescue_off_route_frames = 1
            else:
                self._rescue_off_route_frames += 1
            self._rescue_stuck_frames = 0
            self._rescue_last_pos = None
            if self._rescue_off_route_frames >= self.rescue_stuck_frames:
                LOG.warning(
                    "SELF-RESCUE: character stationary off every recorded layer "
                    "(%d frames at y=%.6f); dropping to layer1 and "
                    "restarting patrol",
                    self._rescue_off_route_frames, pos.y,
                )
                self._rescue_off_route_frames = 0
                self._rescue_off_route_anchor = None
                self._rescue_max_stuck = 0
                self._trigger_rescue()
            return
        self._rescue_off_route_frames = 0
        self._rescue_off_route_anchor = None
        last = self._rescue_last_pos
        if (last is not None
                and abs(pos.x - last.x) < 0.02
                and abs(pos.y - last.y) < 0.02):
            self._rescue_stuck_frames += 1
            self._rescue_max_stuck = max(
                self._rescue_max_stuck, self._rescue_stuck_frames
            )
            # Normal on-route freezes must behave like missing/off-route
            # freezes: rescue at the configured frame threshold, not at the
            # next five-minute bookkeeping window. The rescue worker still
            # performs its movement probe first, so an attack animation or
            # a short reversal handoff cannot cause a false drop/restart.
            if self._rescue_stuck_frames >= self.rescue_stuck_frames:
                LOG.warning(
                    "SELF-RESCUE: character stationary on patrol route for "
                    "%d frames; verifying and restarting patrol",
                    self._rescue_stuck_frames,
                )
                self._rescue_stuck_frames = 0
                self._rescue_max_stuck = 0
                self._rescue_last_pos = None
                self._trigger_rescue()
        else:
            self._rescue_stuck_frames = 0
            # Keep a fixed anchor throughout a no-progress run. Comparing
            # only adjacent frames made legitimate slow walking (<0.02 per
            # frame) look stationary forever despite large total travel.
            self._rescue_last_pos = Point(pos.x, pos.y)

    def _rescue_verify_stuck_by_probe(self) -> bool:
        """True when the character is REALLY stuck and recovery may drop.

        Frequent attacks can hold the character still for longer than the
        stuck window (fixed attack keeps tapping while a monster stands in
        range): the marker freezes and a false self-rescue fires.  Before any
        Alt+Down / patrol-restart the rescue blocks the attack workers and
        forces a short walk right then left; when the marker moves, the
        character was never stuck and the rescue is cancelled - no drop to
        layer1, no patrol restart.
        """
        if (self.reconnect_active_event is not None
                and self.reconnect_active_event.is_set()):
            LOG.info("SELF-RESCUE probe cancelled: auto reconnect owns input")
            return False
        obs = self.last_observation
        if obs is None or obs.player is None:
            # No marker to verify movement against: a missing marker is its
            # own stuck condition and keeps the legacy descent behavior.
            return True
        if not _sender_is_safe(self.key_sender):
            LOG.warning(
                "SELF-RESCUE probe skipped: game window not safely selected; "
                "resuming patrol instead of dropping"
            )
            return False
        self._release_climb_up()
        self._release_walk_hold()
        # Block the attack workers for the whole probe so an attack cannot
        # keep freezing the character or race the probe keys.
        if self.climbing_active_event is not None:
            self.climbing_active_event.set()
        if self.dropping_active_event is not None:
            self.dropping_active_event.set()
        blocked_until = (
            time.monotonic() + self.rescue_probe_attack_block_seconds
        )
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        moved = False
        try:
            for direction in ("right", "left"):
                anchor_obs = self.last_observation
                if anchor_obs is None or anchor_obs.player is None:
                    continue
                anchor = anchor_obs.player.x
                claimed = False
                try:
                    if key_down is None or key_down(direction) is False:
                        break
                    claimed = True
                    time.sleep(min(
                        self.rescue_probe_hold_seconds,
                        max(0.0, blocked_until - time.monotonic()),
                    ))
                finally:
                    if claimed and key_up is not None:
                        key_up(direction)
                time.sleep(self.rescue_probe_settle_seconds)
                current = self.last_observation
                if (current is not None and current.player is not None
                        and abs(current.player.x - anchor)
                        >= self.rescue_probe_move_threshold):
                    LOG.warning(
                        "SELF-RESCUE probe: marker moved %+.6f during the %s "
                        "nudge; character is not stuck",
                        current.player.x - anchor, direction,
                    )
                    moved = True
                    break
        finally:
            # The probe's busy claims end here.  When the rescue then runs
            # its drop/restart those paths manage their own events; when the
            # rescue is cancelled the patrol and attacks simply resume.
            if self.climbing_active_event is not None:
                self.climbing_active_event.clear()
            if self.dropping_active_event is not None:
                self.dropping_active_event.clear()
        if moved:
            return False
        LOG.warning(
            "SELF-RESCUE probe: no marker movement after right/left nudges "
            "(%.1fs attack block); character confirmed stuck",
            self.rescue_probe_attack_block_seconds,
        )
        return True

    def _trigger_rescue(self) -> None:
        """Start the self-rescue in a background thread (guarded once)."""
        if self._rescue_active:
            return
        self._rescue_active = True
        threading.Thread(target=self._run_rescue, name="self-rescue",
                         daemon=True).start()

    def _run_rescue(self) -> None:
        """Recover a stuck patrol without leaving the patrol range.

        Every rescue first VERIFIES the character is genuinely stuck: attacks
        are blocked and the character is forced to walk right then left (see
        ``_rescue_verify_stuck_by_probe``).  A character frozen by frequent
        attacks moves as soon as the attacks stop - the rescue is cancelled
        and no Alt+Down / patrol restart happens.  Only a confirmed stuck
        character reaches the stateful recovery below:

        - On an in-range recorded floor: restart patrol there in place.  A
          stuck in-range character must never be dropped down to the map's
          physical bottom - with an explicit patrol range the bottom floor
          (e.g. layer1) may lie OUTSIDE the range, and the drop would turn a
          recoverable stall into a full below-range return.
        - Below the patrol range (return/climb-to-route active, or a
          detected floor below the range): never drop deeper.  Re-run the
          return climb from a clean state and count the cycle; when the
          return cannot be restored after ``rescue_cycle_limit`` rescues the
          worker stops patrol with an error instead of looping Alt+Down /
          Alt+Up forever at a rope it cannot grab.
        - Otherwise (marker missing / floor unknown): the guarded physical
          descent to the bottom floor re-establishes a known state.
        """
        give_up = False
        try:
            if (self.reconnect_active_event is not None
                    and self.reconnect_active_event.is_set()):
                LOG.info("SELF-RESCUE cancelled: auto reconnect owns input")
                return
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(False)
            # Pre-flight: a character frozen by frequent attacks is NOT stuck.
            if not self._rescue_verify_stuck_by_probe():
                LOG.warning(
                    "SELF-RESCUE aborted: character moves when attacks are "
                    "blocked; resuming patrol (no drop, no restart)"
                )
                return
            if (self.reconnect_active_event is not None
                    and self.reconnect_active_event.is_set()):
                LOG.info("SELF-RESCUE cancelled after probe: auto reconnect owns input")
                return
            obs = self.last_observation
            floor = self._detect_floor_all(obs) if obs is not None else None
            if floor is not None and floor in self._route_layers:
                self._rescue_cycles = 0
                self._restart_patrol_from_first_layer(floor)
                LOG.warning(
                    "SELF-RESCUE: restarting patrol on %s in place (no drop "
                    "out of the patrol range)", floor,
                )
            elif (self._return_mode == "climb-to-route"
                    or (floor is not None
                        and _layer_number(floor) < self._patrol_range_min)):
                self._rescue_cycles += 1
                restart_floor = (
                    floor or self._return_from_floor
                    or self._bottom_recorded_layer()
                )
                LOG.warning(
                    "SELF-RESCUE: below patrol range (on %s); re-running "
                    "return climb without dropping (cycle %d/%d)",
                    restart_floor, self._rescue_cycles,
                    self.rescue_cycle_limit,
                )
                self._restart_patrol_from_first_layer(restart_floor)
                if self._rescue_cycles >= self.rescue_cycle_limit:
                    LOG.error(
                        "SELF-RESCUE: return-to-route failed %d times from "
                        "below the patrol range; stopping patrol (rope climb "
                        "unreachable from the landing spot)",
                        self._rescue_cycles,
                    )
                    give_up = True
                    self._return_mode = None
                    self._return_from_floor = None
                    self._return_arrival_floor = None
                    self._descending_to_first = False
                    self._route_layer_index = None
                    # The patrol is stopping: release every movement key so
                    # nothing stays stuck in the game.
                    self._release_stuck_keys()
                    self._release_climb_up()
                    self._climb_state = ClimbState()
                    with self._patrol_start_lock:
                        self._pending_patrol_start_floor = None
                        self._pending_patrol_start_above_route = False
                    for event in (self.climbing_active_event,
                                  self.dropping_active_event,
                                  self.near_rope_event,
                                  self.moving_active_event):
                        if event is not None:
                            event.clear()
            else:
                self._rescue_cycles = 0
                landed_floor = self._drop_to_first_layer()
                self._restart_patrol_from_first_layer(landed_floor)
        except Exception:
            LOG.exception("self-rescue failed")
        finally:
            if self.patrol_controller is not None:
                reconnect_owns_input = bool(
                    self.reconnect_active_event is not None
                    and self.reconnect_active_event.is_set()
                )
                self.patrol_controller.set_enabled(
                    False if reconnect_owns_input else not give_up
                )
            self._rescue_active = False

    def _rope_approach_stalled(
        self, player_x: float, rope_x: Optional[float], route_label: str
    ) -> bool:
        """True when the rope-approach walk makes no X progress while the
        character is aligned with the rope.

        The character is then ON the rope mid-height (pressing left/right
        there does not move it), so the walk+Z approach would loop forever.
        """
        if self._rope_approach_phase_label != route_label:
            self._rope_approach_phase_label = route_label
            self._rope_approach_last_x = player_x
            self._rope_approach_stall_frames = 0
            self._rope_approach_far_stall_frames = 0
            self._rope_approach_far_stall_count = 0
            return False
        last_x = self._rope_approach_last_x
        self._rope_approach_last_x = player_x
        if last_x is None:
            return False
        moved = abs(player_x - last_x) >= 0.002
        aligned = (
            rope_x is not None
            and abs(player_x - rope_x) <= ROPE_STALL_ALIGNMENT_RANGE
        )
        if moved:
            self._rope_approach_stall_frames = 0
            self._rope_approach_far_stall_frames = 0
            self._rope_approach_far_stall_count = 0
            return False
        if not aligned:
            # A monster hit can leave the game ignoring an already-held
            # direction key while the marker is still on the platform. This
            # is separate from near-rope recovery: far from the rope we only
            # re-arm walking, never jump-climb.
            self._rope_approach_stall_frames = 0
            self._rope_approach_far_stall_frames += 1
            return False
        self._rope_approach_far_stall_frames = 0
        self._rope_approach_stall_frames += 1
        # 卡在边缘且 X 不动时尽快起跳（2 帧，约 0.2-0.5s）：拖太久角色
        # 会一直停在边缘刷原地。
        return self._rope_approach_stall_frames >= 2

    def _recover_rope_approach(
        self, observation: MinimapObservation, rope_x: Optional[float]
    ) -> None:
        """Rope-approach stall recovery (two distinct cases).

        The walk toward the rope is not advancing:
        - the character is ON the rope (marker Y not on any platform band,
          or within ~1 minimap px of the rope X) -> climb Up;
        - the character is blocked at the PLATFORM EDGE right next to the
          rope (it just missed the jump gate) -> jump toward the rope and
          let the climb state machine handle the grab/retry.
        """
        if rope_x is None or observation.player is None:
            return
        if self._climb_state.phase != "idle":
            return  # already climbing
        # Stuck at the rope area while a walk was issued: clear any game-side
        # stuck key (lost key-up during a knock-down) before the climb/jump
        # recovery below takes over.
        self._release_stuck_keys()
        gap = rope_x - observation.player.x
        if abs(gap) > ROPE_STALL_ALIGNMENT_RANGE:
            # Defensive second gate: callers must never convert an ordinary
            # walk across the platform into a jump-climb loop.  This also
            # protects against future call-site mistakes or a rope target
            # that changes after the stall samples were collected.
            self._rope_approach_stall_frames = 0
            LOG.warning(
                "ROPE STUCK recovery ignored: rope is still %.4f away; "
                "continuing platform approach",
                abs(gap),
            )
            return
        on_rope = self._detected_layer(observation) is None
        if on_rope or abs(gap) <= 0.01:
            self._start_rope_stuck_climb(observation)
            return
        direction = "right" if gap > 0 else "left"
        LOG.warning("ROPE STUCK recovery: blocked at platform edge near the "
                    "rope (gap=%.4f); jumping %s toward it", gap, direction)
        self._run_climb_step(observation, rope_x, direction)

    def _start_rope_stuck_climb(self, observation: MinimapObservation) -> None:
        """The character is ON the rope mid-height: start an attached climb.

        Holds Up and hands control to the climb state machine (which defers
        all left/right walks - so NO Z is pressed while on the rope) until
        the character climbs to the top and steps onto the platform.
        """
        if self._climb_state.phase != "idle":
            return  # already climbing
        state = self._climb_state
        state.phase = "climbing-up"
        state.up_held = True
        state.baseline_y = (
            observation.player.y if observation.player is not None else None
        )
        world_ok = bool(
            observation.world_y_diamonds is not None
            and observation.structure_confidence is not None
            and observation.structure_confidence >= 0.12
        )
        state.baseline_world_y = (
            observation.world_y_diamonds if world_ok else None
        )
        state.last_world_y = state.baseline_world_y
        state.last_marker_y = state.baseline_y
        state.attach_frames = 2  # already attached to the rope
        state.stalled_frames = 0
        state.arrival_frames = 0
        with self._direction_lock:
            self.key_sender.key_down("up")
        if self.climbing_active_event is not None:
            self.climbing_active_event.set()
        self._rope_stuck_recoveries += 1
        LOG.warning("ROPE STUCK recovery #%d: character on the rope; "
                    "climbing up (Z paused)", self._rope_stuck_recoveries)

    def _movement_busy_now(self) -> bool:
        """True while vertical recovery or a confirmed stair action is active.

        The post-arrival timestamp still suppresses unsafe stair jumps, but it
        must not suppress attacks after the new layer has been confirmed.
        """
        if (self.dropping_active_event is not None
                and self.dropping_active_event.is_set()):
            return True
        if self._climb_state.phase != "idle":
            return True
        stair_jump_active = getattr(self.stair_jump_worker, "is_active", None)
        if callable(stair_jump_active) and stair_jump_active():
            # Keep the hold manager from dropping the ordinary Left/Right
            # claim just because an attack animation is still being observed.
            # The dedicated worker will add one short Alt tap on top of it.
            return True
        return False

    def _release_walk_hold(self) -> None:
        """Release the currently held walk direction and any legacy Z hold."""
        key_up = getattr(self.key_sender, "key_up", None)
        with self._direction_lock:
            with self._hold_lock:
                if self._walk_hold_key is not None:
                    if key_up is not None:
                        key_up(self._walk_hold_key)
                    self._walk_hold_key = None
                if self._walk_hold_z:
                    if key_up is not None:
                        key_up("z")
                    self._walk_hold_z = False
                    if self.pickup_active_event is not None:
                        self.pickup_active_event.clear()
                    LOG.info("pickup: Z released with walk")
                self._walk_hold_until = 0.0

    def _hold_manager(self) -> None:
        """Release the walk key when its hold deadline passes or the attack
        takes over (with the busy gate).  Runs in its own thread so the main
        loop keeps processing minimap frames during a walk hold."""
        while not self.stop_event.is_set():
            time.sleep(0.02)
            try:
                if self._patrol_abort_event.is_set():
                    # The sender's lifecycle scrub owns the real key-up.  Do
                    # not let this asynchronous manager emit a second, late
                    # release after the operator has resumed manual input.
                    with self._direction_lock, self._hold_lock:
                        self._walk_hold_key = None
                        self._walk_hold_z = False
                        self._walk_hold_until = 0.0
                    continue
                release = False
                # Watchdog: the direction-handoff reservation is held for at
                # most ~1.2s in normal patrol.  If one outlives that (an
                # interrupted handoff), every attack would be skipped with
                # "patrol direction handoff is active", so clear it.
                handoff = self.direction_transition_event
                if handoff is not None and handoff.is_set():
                    self._transition_stuck_frames += 1
                    if self._transition_stuck_frames > 75:
                        LOG.warning(
                            "direction handoff reservation stuck for %.1fs; "
                            "clearing it so attacks resume",
                            self._transition_stuck_frames * 0.02,
                        )
                        handoff.clear()
                        self._transition_stuck_frames = 0
                else:
                    self._transition_stuck_frames = 0
                with self._hold_lock:
                    if self._walk_hold_key is None:
                        continue
                    now = time.monotonic()
                    release = now >= self._walk_hold_until
                    if not release and self._attack_state is not None:
                        if (not self._movement_busy_now()
                                and self._attack_state.is_active()):
                            if self._attack_active_since is None:
                                self._attack_active_since = now
                            if (now - self._attack_active_since
                                    <= self.attack_block_max_seconds):
                                LOG.info(
                                    "walk key released early: attack took over"
                                )
                                release = True
                        else:
                            self._attack_active_since = None
                # Do not acquire the directional lock while holding
                # _hold_lock: 小碎步 takes them in the opposite order while it
                # clears patrol, and lock inversion would deadlock input.
                if release:
                    self._release_walk_hold()
            except Exception:
                LOG.exception("hold manager failed")

    def _stall_watchdog(self) -> None:
        """Report a movement worker that stops reacting while patrol is armed.

        Observed failure: the character freezes and no movement line reaches
        the log at all, while the attack worker keeps running.  That means
        either this worker's thread is wedged (blocked on a lock, or dead) or
        its state produced no key - and only restarting the assistant
        recovers it. The stalled worker's own stack names the blocked frame;
        other stacks are debug-only because timed waits are normal idle state.
        """

        while not self.stop_event.wait(STALL_WATCHDOG_INTERVAL_SECONDS):
            if (self.automation_active_event is None
                    or not self.automation_active_event.is_set()):
                self._stall_reported = False
                continue
            last = self._last_frame_at
            if last is None:
                continue
            silent = time.monotonic() - last
            if silent < STALL_WATCHDOG_SECONDS:
                self._stall_reported = False
                continue
            if self._stall_reported:
                continue
            self._stall_reported = True
            self._report_stall(silent)

    def _report_stall(self, silent: float) -> None:
        """Log the stalled movement state without misreporting idle workers."""

        LOG.error(
            "movement worker silent for %.1fs while patrol input is armed "
            "(thread alive=%s patrol_enabled=%s return_mode=%s route_phase=%s "
            "route_state=%s walk_hold=%s handoff_reserved=%s) - capturing "
            "movement stack",
            silent,
            self.is_alive(),
            self.patrol_enabled,
            self._return_mode,
            self._route_phase,
            self._route_target_label,
            self._walk_hold_key,
            (self.direction_transition_event.is_set()
             if self.direction_transition_event is not None else None),
        )
        frames = sys._current_frames()
        own = frames.get(self.ident)
        if own is not None:
            # This is the only stack that represents a possible failure. A
            # focus/supervisor thread parked in Event.wait is healthy.
            LOG.error(
                "thread %s (the stalled movement worker) stack:\n%s",
                self.name,
                "".join(traceback.format_stack(own)),
            )
        else:
            LOG.error(
                "movement watchdog could not capture its own stack "
                "(thread is no longer registered)"
            )
        for thread in threading.enumerate():
            if thread.ident == self.ident:
                continue
            frame = frames.get(thread.ident)
            if frame is None:
                continue
            LOG.debug(
                "movement-watchdog diagnostic snapshot; thread %s stack:\n%s",
                thread.name,
                "".join(traceback.format_stack(frame)),
            )

    def _release_stuck_keys(self) -> None:
        """Release EVERY key the bot may be holding at a stuck detection.

        A knock-down / focus dip can make the game MISS a key-up, so the game
        keeps an OLD key pressed - walking right, holding Up on a rope,
        pickup Z, Alt - even though the sender already released it.  At a
        stuck detection all movement keys are re-released: unconditional
        key-ups for the full movement set (clears game-side stuck presses the
        sender no longer owns) plus the sender's release-all (clears whatever
        the bot still owns).  The next frame's decisions re-press only what
        is actually needed.  Never runs on a timer - stuck events only.
        """

        with self._direction_lock:
            force_key_up = getattr(self.key_sender, "force_key_up", None)
            key_up = getattr(self.key_sender, "key_up", None)
            if callable(force_key_up):
                for key in ("up", "down", "left", "right", "alt", "z"):
                    force_key_up(key, reason="movement recovery")
            elif key_up is not None:
                for key in ("up", "down", "left", "right", "alt", "z"):
                    key_up(key)
            release_all = getattr(self.key_sender, "release_all_keys", None)
            if callable(release_all):
                release_all()

    def _send_walk_hold(self, decision: MovementDecision) -> bool:
        """Schedule a direction-key hold: press now, release after the
        duration (or early on attack) via the hold-manager thread.

        The main loop is NOT blocked - it keeps processing minimap frames,
        so the stair/pit stall detection runs at the minimap frame rate
        (10 frames ~= 2.5s) instead of being delayed by the hold.  Movement
        and the jump (Alt tap) can overlap: the jump is a short chord on
        top of the held direction.

        Pickup is tied to the walk: Z goes down with the direction and comes
        up with it.  Only called for plain left/right walks (move-to-left /
        move-to-right / move-to-rope walk) - never stair jumps or
        jump-to-rope.
        """

        if not self._patrol_input_allowed():
            return False
        if decision.key not in ("left", "right"):
            return _send_tap(self.key_sender, decision)
        if not _sender_is_safe(self.key_sender):
            LOG.warning("movement suppressed: target window is not safely selected")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        if key_down is None:
            return _send_tap(self.key_sender, decision)
        direction_transition_started = False
        try:
            with self._direction_lock, self._hold_lock:
                if not self._patrol_input_allowed():
                    return False
                # The focus worker releases all physical keys on a focus dip.  Its
                # release happens outside this hold state, so the worker can still
                # believe Right/Z are held after refocus and silently skip their
                # key-downs (observed: repeated action=right with frozen X and no
                # key-down log). Reconcile with the sender's authoritative owner
                # table before extending an existing hold.
                is_key_down = getattr(self.key_sender, "is_key_down", None)
                if callable(is_key_down):
                    if (self._walk_hold_key is not None
                            and not is_key_down(self._walk_hold_key)):
                        LOG.info(
                            "walk hold %s was externally released; re-arming",
                            self._walk_hold_key,
                        )
                        self._walk_hold_key = None
                    if self._walk_hold_z and not is_key_down("z"):
                        self._walk_hold_z = False
                        if self.pickup_active_event is not None:
                            self.pickup_active_event.clear()
                if self._walk_hold_key != decision.key:
                    previous = self._walk_hold_key
                    # 换方向：先松开旧键再按新键。
                    if previous in ("left", "right"):
                        transition_event = self.direction_transition_event
                        if transition_event is not None:
                            # Reserve the complete handoff before releasing the
                            # old key. A fixed attack must not enter between
                            # Left-up and Right-down (or vice versa).
                            transition_event.set()
                            direction_transition_started = True
                        # If an attack started just before the reservation, wait
                        # out its arbiter grace before sending the opposite walk
                        # key. Otherwise the game can consume that new key as a
                        # continuation of the attack animation.
                        attack_motion_active = getattr(
                            self.motion_arbiter, "attack_motion_active", None
                        )
                        if callable(attack_motion_active):
                            deadline = time.monotonic() + 1.0
                            while (attack_motion_active()
                                   and time.monotonic() < deadline):
                                if not self._wait_for_patrol_motion(0.02):
                                    if transition_event is not None:
                                        transition_event.clear()
                                    return False
                    self._release_walk_hold()
                    if previous in ("left", "right"):
                        # Endpoint turns are where a lost old key-up is most
                        # harmful, so send one unconditional release as a
                        # safeguard.  It MUST happen before the new direction
                        # goes down: Maple can treat a late Right-up after a
                        # Left-down (or vice versa) as "no horizontal input",
                        # which produced the observed stall immediately after a
                        # successful endpoint turn.
                        force_key_up = getattr(self.key_sender, "force_key_up", None)
                        key_up_old = getattr(self.key_sender, "key_up", None)
                        if callable(force_key_up):
                            force_key_up(previous, reason="walk direction switch")
                        elif key_up_old is not None:
                            key_up_old(previous)
                        # Maple can drop a newly pressed opposite direction when
                        # it arrives in the same input-poll slice as the old
                        # direction's key-up.  Keep a short neutral interval so
                        # every endpoint turn is received as Left-up -> pause ->
                        # Right-down (or the reverse), rather than two events at
                        # the identical timestamp.
                        if not self._wait_for_patrol_motion(
                            DIRECTION_SWITCH_NEUTRAL_GAP_SECONDS
                        ):
                            if self.direction_transition_event is not None:
                                self.direction_transition_event.clear()
                            return False
                    if not self._patrol_input_allowed():
                        if self.direction_transition_event is not None:
                            self.direction_transition_event.clear()
                        return False
                    claimed = key_down(decision.key) is not False
                    if not claimed:
                        if self.direction_transition_event is not None:
                            self.direction_transition_event.clear()
                        LOG.info(
                            "walk key %s send blocked (window not foreground "
                            "or input disabled) - character will not move",
                            decision.key,
                        )
                        return False
                    self._walk_hold_key = decision.key
                # Automatic pickup no longer sends Z.  Ctrl+Z is the sole
                # pickup input and belongs to QuickPickupWorker, so movement
                # holds only the requested direction.
                self._walk_hold_until = time.monotonic() + max(
                    0.01, float(decision.duration)
                )
                if (self.stationary_attack_enabled
                        and self._walk_hold_key in ("left", "right")):
                    # 站桩攻击 records the direction the character was really
                    # turned to: the recovery walk toward the anchor is what
                    # leaves it facing away from the selected 朝向.  Recording
                    # it here (where the key went down) instead of at the
                    # decision keeps a walk that the movement cooldown skipped
                    # or the input layer refused from looking like a turn.
                    self._stationary_facing_command = self._walk_hold_key
            if direction_transition_started:
                # Let the new direction settle for a game input tick before
                # the attack worker can send another action key.
                self._wait_for_patrol_motion(0.15)
            return True
        finally:
            # The handoff reservation must never outlive this call: a key
            # send that raised (for example a SendInput failure) used to
            # leave the event set, so every later attack was skipped with
            # "patrol direction handoff is active" until a restart.
            if (direction_transition_started
                    and self.direction_transition_event is not None):
                self.direction_transition_event.clear()

    def __init__(
        self,
        frame_queue: "queue.Queue[Any]",
        key_sender: Any,
        stop_event: threading.Event,
        *,
        minimap_region: tuple[float, float, float, float] = DEFAULT_MINIMAP_REGION,
        minimum_confidence: float = 0.55,
        movement_cooldown: float = 0.25,
        fixed_target_x: Optional[float] = None,
        horizontal_tolerance: float = 0.010,
        horizontal_tolerance_diamonds: Optional[float] = None,
        climb_up_hold_seconds: float = 0.45,
        movement_hold_seconds: float = 2.0,
        minimum_final_hold_seconds: float = 0.08,
        minimum_movement_hold_seconds: float = 0.30,
        estimated_minimap_speed: float = 0.11,
        final_calculation_distance: float = 0.04,
        final_calculation_diamonds: Optional[float] = None,
        estimated_final_speed: float = 0.205,
        final_move_safety_gain: float = 0.95,
        aligned_frames_required: int = 3,
        climb_layer_confirm_frames: int = 4,
        climb_layer_confirm_seconds: float = 0.3,
        climb_arrival_world_tolerance: float = 0.20,
        climb_nudge_seconds: float = 0.10,
        climb_y_change_required: float = 0.015,
        climb_world_y_change_required: float = 0.75,
        climb_world_y_stall_change_required: float = 0.15,
        climb_world_y_stall_frames: int = 3,
        climb_failed_shift_right_seconds: float = 0.01,
        climb_attempt_interval_seconds: float = 1.0,
        climb_failed_cycles_reset: int = 3,
        # Consecutive under-rope sideways climb-jump failures before the rope
        # approach restarts from left-most (walk away, re-approach from the
        # edge) - same ladder as climb_failed_cycles_reset but for the
        # lateral recovery, which is NOT reset by the walk back to the rope.
        climb_lateral_cycles_reset: int = 3,
        patrol_cycles_per_layer: int = 2,
        near_rope_seconds: float = 0.5,
        near_rope_range: Optional[float] = None,
        near_rope_inner_range: Optional[float] = None,
        near_rope_diamonds: Optional[float] = None,
        under_rope_tolerance: float = 0.008,
        climb_attack_lock: Optional[threading.Lock] = None,
        direction_transition_event: Optional[threading.Event] = None,
        climbing_active_event: Optional[threading.Event] = None,
        stationary_recovery_active_event: Optional[threading.Event] = None,
        stationary_attack_resume_event: Optional[threading.Event] = None,
        dropping_active_event: Optional[threading.Event] = None,
        near_rope_event: Optional[threading.Event] = None,
        moving_active_event: Optional[threading.Event] = None,
        pickup_active_event: Optional[threading.Event] = None,
        important_positions: Optional[dict[str, Any]] = None,
        route_order: Optional[list[str]] = None,
        patrol_enabled: bool = True,
        climbing_enabled: bool = True,
        final_layer_action: str = "wait",
        first_layer: Optional[str] = None,
        # Contiguous patrol floor range: only these floors are patrolled and
        # the character returns to the range whenever it falls outside it.
        # ``layer1`` is no longer implicitly the patrol start - any recorded
        # floor can begin (or be the only) patrol floor.
        patrol_start_layer: Optional[str] = None,
        patrol_end_layer: Optional[str] = None,
        # Fall detection for ``FALL RECOVERY``: the diamond Y dropping fast
        # for this many consecutive frames (outside an intentional drop or
        # climb) counts as an unexpected fall; when it stops the floor is
        # re-detected and patrol restarts there (or the character returns to
        # the patrol range).
        fall_detect_frames: int = 4,
        fall_marker_y_gain: float = 0.015,
        # Landing reconciliation after a fall/knock-down: the raw marker Y is
        # screen-relative on a scrolling minimap and the OpenCV world-Y
        # tracker lags fast vertical motion, so the landing floor is resolved
        # from world-Y samples only after they stabilize, then the tracker is
        # re-anchored to the true layer (cancelling lag/drift).
        fall_settle_min_frames: int = 4,
        fall_settle_epsilon: float = 0.15,
        fall_settle_max_seconds: float = 1.2,
        # World-Y drift watchdog: while cruising on a believed floor the
        # tracker's incremental correlation can drift over time; when the raw
        # world Y drifts from the floor's expected anchor beyond this bound
        # the tracker is silently re-anchored.
        world_drift_check_interval_seconds: float = 2.0,
        world_drift_reanchor_threshold: float = 0.35,
        drop_chord_hold_seconds: float = 0.10,
        drop_retry_seconds: float = 1.0,
        minimap_detector: Any = None,
        patrol_controller: Any = None,
        diamond_size_tracker: Optional[DiamondSizeTracker] = None,
        structure_tracker: Any = None,
        automation_active_event: Optional[threading.Event] = None,
        # 自动重连 sets this while it owns the keyboard.  Only the falling edge is used: the first
        # frame after patrol input is re-armed checks whether the character is still on the patrol
        # route and, when he is not, starts the return-to-route instead of waiting for the
        # throttled verifier.
        reconnect_active_event: Optional[threading.Event] = None,
        motion_arbiter: Any = None,
        attack_state_path: Optional[str] = None,
        attack_block_max_seconds: float = 4.0,
        rope_state_path: Optional[str] = None,
        patrol_state_path: Optional[str] = None,
        patrol_busy_hold: float = 3.0,
        rope_jump_px: float = 140.0,
        on_rope_px: float = 50.0,
        under_rope_px: float = 10.0,
        rope_approach_creep_seconds: float = 0.25,
        rope_tiny_step_min_seconds: float = 0.05,
        rope_tiny_step_max_seconds: float = 0.15,
        small_step_face_left: bool = False,
        small_step_left_first: Optional[bool] = None,
        yolo_detection_active: bool = True,
        other_player_check_enabled: bool = False,
        other_player_check_interval_seconds: float = 60.0,
        rescue_check_interval_seconds: float = 300.0,
        rescue_stuck_frames: int = 25,
        # Consecutive self-rescue cycles that end below the patrol range
        # (return-to-route could not be restored): after this many the worker
        # stops patrol instead of dropping/retrying forever from a spot whose
        # rope climb cannot grab.
        rescue_cycle_limit: int = 3,
        # Self-rescue pre-flight: frequent attacks can freeze the character
        # past the stuck window (fixed attack keeps tapping while a monster
        # stands in range), so before ANY drop/restart the rescue blocks the
        # attacks and forces a short walk right then left to verify the
        # character is really stuck.  These tune that probe.
        rescue_probe_attack_block_seconds: float = 2.0,
        rescue_probe_hold_seconds: float = 0.7,
        rescue_probe_settle_seconds: float = 0.2,
        rescue_probe_move_threshold: float = 0.006,
        other_player_drug_taps: int = 3,
        other_player_drug_gap_seconds: float = 1.0,
        other_player_hp_threshold: float = 0.70,
        other_player_switch_max_attempts: int = 3,
        other_player_switch_settle_seconds: float = 1.0,
        other_player_request_message: str = "",
        other_player_room_code: str = "",
        other_player_wait_minutes: float = 5.0,
        current_channel: int = 1,
        on_channel_landed: Any = None,
        status_state_path: Optional[str] = None,
        drug_settings_path: Optional[str] = None,
        stair_jump_enabled: bool = True,
        stair_jump_stall_diamonds: float = 0.25,
        stair_jump_stall_frames: int = 9,
        patrol_start_grace_seconds: float = 3.0,
        stair_jump_attempts_max: int = 1,
        # 台阶/坑边尝试间隔 0.8s（原 2.5s）：地图坑多时角色卡在边缘会等
        # 很久才跳下一次——缩短间隔让角色更快跳出坑/边缘。
        stair_jump_grace_seconds: float = 0.8,
        stair_jump_alt_hold_seconds: float = 0.06,
        stair_jump_lead_seconds: float = 0.15,
        stair_jump_climb_arrival_grace_seconds: float = 2.0,
        character_positions: Optional[queue.Queue] = None,
    ) -> None:
        super().__init__(name="movement-worker", daemon=True)
        self.frame_queue = frame_queue
        self.character_positions = character_positions
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.minimap_region = minimap_region
        self.minimum_confidence = minimum_confidence
        self.movement_cooldown = movement_cooldown
        self.fixed_target_x = fixed_target_x
        self.horizontal_tolerance = horizontal_tolerance
        self.horizontal_tolerance_diamonds = (
            float(horizontal_tolerance_diamonds)
            if horizontal_tolerance_diamonds is not None else None
        )
        self._current_horizontal_tolerance = horizontal_tolerance
        self.climb_up_hold_seconds = climb_up_hold_seconds
        self.movement_hold_seconds = movement_hold_seconds
        self.minimum_final_hold_seconds = minimum_final_hold_seconds
        self.minimum_movement_hold_seconds = minimum_movement_hold_seconds
        self.estimated_minimap_speed = estimated_minimap_speed
        self.final_calculation_distance = final_calculation_distance
        self.final_calculation_diamonds = (
            float(final_calculation_diamonds)
            if final_calculation_diamonds is not None else None
        )
        self._current_final_calculation_distance = final_calculation_distance
        self.estimated_final_speed = estimated_final_speed
        self.final_move_safety_gain = final_move_safety_gain
        self.aligned_frames_required = max(2, aligned_frames_required)
        self.climb_layer_confirm_frames = max(2, int(climb_layer_confirm_frames))
        self.climb_layer_confirm_seconds = max(
            0.0, float(climb_layer_confirm_seconds)
        )
        self.climb_arrival_world_tolerance = max(
            0.01, float(climb_arrival_world_tolerance)
        )
        self.climb_nudge_seconds = climb_nudge_seconds
        self.climb_y_change_required = climb_y_change_required
        self.climb_world_y_change_required = climb_world_y_change_required
        self.climb_world_y_stall_change_required = climb_world_y_stall_change_required
        self.climb_world_y_stall_frames = max(1, int(climb_world_y_stall_frames))
        self.climb_failed_shift_right_seconds = climb_failed_shift_right_seconds
        # Fresh climb attempts are rate-limited to this interval; an
        # in-progress climb state machine advances every frame instead.
        self.climb_attempt_interval_seconds = max(
            0.2, float(climb_attempt_interval_seconds)
        )
        # Consecutive full failed climb cycles before the route restarts at
        # left-most and re-approaches the rope (see _climb_cycle_failed).
        self.climb_failed_cycles_reset = max(1, int(climb_failed_cycles_reset))
        self.patrol_cycles_per_layer = max(1, int(patrol_cycles_per_layer))
        self._climb_failures = 0
        # 同一层爬楼反复失败的"重置回最左"次数：超过上限后升级为完整自救
        # （回第一层 + 重启 + 重锚定），避免无限在同一层巡逻不爬楼。
        self._climb_restarts = 0
        # Under-rope lateral recovery state: the side to try next (alternates
        # after every sideways climb jump) and the consecutive-failure streak
        # that restarts the rope approach once the rope cannot be reached
        # even from the side (stairs/bench hops included).
        self.climb_lateral_cycles_reset = max(
            1, int(climb_lateral_cycles_reset)
        )
        self._climb_lateral_side = "left"
        self._climb_lateral_streak = 0
        # 爬楼失败时记录上一次的标记 X，用于检测"X 冻结"（绳子不可达）。
        self._climb_last_x: Optional[float] = None
        self.near_rope_seconds = near_rope_seconds
        self.near_rope_range = near_rope_range
        self.near_rope_inner_range = (
            float(near_rope_inner_range)
            if near_rope_inner_range is not None else None
        )
        self.near_rope_diamonds = (
            float(near_rope_diamonds) if near_rope_diamonds is not None else None
        )
        # |minimap gap| at which the character counts as DIRECTLY under the
        # rope: jump straight up (Alt+Up) instead of a left/right chord, so
        # the sideways jump cannot shove the character past the rope.
        self.under_rope_tolerance = max(
            0.0, min(float(under_rope_tolerance), 0.05)
        )
        self.climb_attack_lock = climb_attack_lock
        self.direction_transition_event = direction_transition_event
        self._transition_stuck_frames = 0
        # Frame heartbeat + stall reporting (see _stall_watchdog).
        self._last_frame_at: Optional[float] = None
        self._stall_reported = False
        self._route_target_label: Optional[str] = None
        self._marginal_endpoint_key: Optional[tuple] = None
        self._marginal_endpoint_frames = 0
        # (layer, phase, target) progress tracker for the far-stall bound.
        self._progress_key: Optional[tuple] = None
        self._progress_best = 0.0
        self._progress_frames = 0
        self.climbing_active_event = climbing_active_event
        self.stationary_recovery_active_event = stationary_recovery_active_event
        self.stationary_attack_resume_event = stationary_attack_resume_event
        self._stationary_attack_was_blocked = False
        self.dropping_active_event = dropping_active_event
        self.near_rope_event = near_rope_event
        self.important_positions = important_positions or {}
        # A layer patrols with any non-empty subset of Left/Rope/Right; a
        # layer with NO recorded action (or an empty profile) stands still
        # and only attacks.
        routable = {
            name for name, value in self.important_positions.items()
            if _layer_present_actions(value)
        }
        if route_order is not None:
            self._route_layers = [
                name for name in route_order if name in routable
            ]
            # Bottom-up by numeric suffix - never by recording order (a top
            # layer recorded before a lower one must not patrol first).
            self._route_layers.sort(
                key=lambda name: int(
                    "".join(filter(str.isdigit, name)) or 0
                ),
            )
        else:
            self._route_layers = sorted(
                routable,
                key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
            )
        # Patrol only the selected contiguous floor range.  ``patrol_start_layer``
        # defaults to the bottom recorded floor and ``patrol_end_layer`` to the
        # top recorded floor when unset, so legacy configs keep the old route.
        self._route_layers = _slice_patrol_range(
            self._route_layers, patrol_start_layer, patrol_end_layer
        )
        self._patrol_range_min, self._patrol_range_max = _patrol_range_numbers(
            self._route_layers, patrol_start_layer, patrol_end_layer
        )
        # Whether the UI explicitly configured a patrol floor range (both
        # bounds selected).  An explicit range always loops: its TOP floor
        # drops back to its FIRST floor once the patrol there finishes.
        self._patrol_range_configured = bool(
            patrol_start_layer and patrol_end_layer
        )
        self._route_layer_index: Optional[int] = None
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        # Start Patrol performs its own focused capture and floor detection.
        # Transfer that result into this worker on its next frame so a stale
        # Stop/Start climb, return, or route index cannot survive the restart.
        self._patrol_start_lock = threading.Lock()
        self._pending_patrol_start_floor: Optional[str] = None
        self._pending_patrol_start_above_route = False
        self._pending_patrol_start_reconnect = False
        # A failed stair approach can force the route to reverse direction.
        # Do not immediately count that new endpoint as reached while the
        # marker is still standing at the blocked position.
        self._forced_phase_entry: Optional[tuple[int, str, float]] = None
        self.patrol_enabled = patrol_enabled
        self.climbing_enabled = climbing_enabled
        self.final_layer_action = final_layer_action
        # The patrol start is the range start (``layer1`` is no longer
        # implied): prefer the explicitly selected range start, then the
        # legacy override, then the route bottom.
        self.first_layer = (
            patrol_start_layer
            or first_layer
            or (self._route_layers[0] if self._route_layers else None)
        )
        self.drop_chord_hold_seconds = drop_chord_hold_seconds
        self.drop_retry_seconds = drop_retry_seconds
        self.minimap_detector = minimap_detector
        self.patrol_controller = patrol_controller
        self.diamond_size_tracker = diamond_size_tracker
        self.structure_tracker = structure_tracker
        self.automation_active_event = automation_active_event
        # A patrol stop is a stronger edge than the next capture frame.  It
        # wakes any in-flight direction handoff / micro-step immediately so a
        # stale action cannot finish after the operator has stopped patrol.
        self._patrol_abort_event = threading.Event()
        # 自动重连 window tracking (see ROUTE_CHECK_AFTER_RECONNECT_SECONDS).  The event being SET
        # is only remembered; its falling edge arms ``_route_check_pending``, which then waits
        # (bounded by ``_route_check_deadline``) for one usable marker reading.
        self.reconnect_active_event = reconnect_active_event
        self._reconnect_was_active = False
        self._route_check_pending = False
        self._route_check_deadline = 0.0
        # Optional in tests/headless integrations.  The normal assistant
        # injects the shared arbiter so confirmed stair jumps can queue
        # behind attack motions.
        self.motion_arbiter = motion_arbiter
        # Stair detection stays on this minimap-consuming worker, but the
        # direction+Alt action runs on its own worker.  It waits for attack
        # motion without making patrol release its current Left/Right hold.
        self.stair_jump_worker: Any = None
        # A registration is followed by a fixed five-frame detection quiet
        # period.  This spans brief route-label changes and marker jitter
        # after the hop, so one stair event cannot immediately register more
        # Alt presses.
        self._stair_jump_skip_frames = 0
        self._stair_jump_completion_lock = threading.Lock()
        self._stair_jump_completion: Optional[bool] = None
        # A jump point fires once per LEG - one directional traversal of one
        # route floor - and is armed again by the next leg or the next Start
        # Patrol pass.  It is NOT re-armed merely because the marker left its
        # zone: the zone is deliberately loose, so a landing, a walk back over
        # the same spot, or a climb past the same row re-enters it at once, and
        # the field report is a 右跳 that mounted the rope at layer2 and then
        # fired the same 右跳 again on another run - throwing the character off
        # the rope it had just grabbed.
        self._jump_point_inside_tokens: set[tuple[str, int]] = set()
        # A fired point stays suppressed for its entire Up-hold/landing
        # session.  A jump arc naturally leaves and re-enters the loose Y
        # tolerance; treating that as a new arrival created repeated jumps.
        self._jump_point_suppressed_tokens: set[tuple[str, int]] = set()
        # token -> (pass_id, leg_key, monotonic time) of its last dispatch.
        self._jump_point_fired: dict[
            tuple[str, int], tuple[int, tuple[int, int, str], float]
        ] = {}
        # floor -> last logged rope-target answer, so the per-frame target
        # computation cannot flood the running log.
        self._rope_target_answers: dict[str, str] = {}
        # Incremented by every new movement pass (Start Patrol, 站桩 manual
        # start, each pickup circuit).  A pass change arms every point again.
        self._jump_point_pass_id = 0
        # A coordinate match is only a candidate.  It becomes dispatched after
        # the dedicated jump worker accepts the request.
        self._jump_point_candidate: Optional[tuple[str, int]] = None
        self._jump_point_up_held = False
        self._jump_point_up_started_at = 0.0
        self._jump_point_y_samples: list[float] = []
        # First post-jump marker row.  A stable reading alone is not a
        # landing: a missed jump also settles back on its departing platform.
        self._jump_point_start_y: Optional[float] = None
        self._jump_point_last_y: Optional[float] = None
        self._jump_point_rise_frames = 0
        # Set only when this recorded point was dispatched while the active
        # route target was MOVE TO ROPE.  A horizontal stair point must never
        # manufacture a rope-climb handoff merely because its Y rises.
        self._jump_point_rope_handoff = False
        # True once the automation input gate has been observed active, so the
        # disarmed->armed edge (Start Patrol) can be detected exactly once.
        self._automation_was_active = False
        # Sender generations change whenever Stop, focus protection, or Start
        # performs a forced keyboard scrub.  Local walk/climb bookkeeping from
        # an older generation must never attempt to release a newer key claim.
        self._input_session_seen: Optional[int] = None
        # Set while the character is actively walking (left/right decisions),
        # used by the pickup worker to only tap Z during movement.
        self.moving_active_event = moving_active_event
        # Set while the pickup worker physically holds Z.  Climb/jump/drop
        # keys wait for it to clear so Z can never overlap the Up hold and
        # interrupt a rope grab (a Z keydown fires a skill even for a few ms).
        self.pickup_active_event = pickup_active_event
        self._pickup_z_force_after = 0.0
        # Z pickup counter (pickup now rides the route-walk key holds).
        self._pickup_count = 0
        # 非阻塞行走 hold：主循环不再被方向键 hold 阻塞，方向键的按住/
        # 松开交给独立的 hold 管理线程（_hold_manager）。这样主循环按
        # 最小地图帧率跑，卡住检测（10 帧 ≈ 2.5s）不会被 2 秒 hold 拖延。
        self._hold_lock = threading.RLock()
        # Every L/R/U/D action takes this lock.  Normal patrol only holds it
        # while changing a key; rope/stair/drop and 小碎步 retain it for their
        # full chord so their directions cannot cross.
        self._direction_lock = threading.RLock()
        # Compatibility for pre-v1.0.75 callers.  Small-step now always ends
        # right; facing after a stand-still recovery is its own option.
        if small_step_left_first is not None:
            # Compatibility for callers written before the facing control:
            # the former value chose the FIRST direction, while the current
            # value chooses the FINAL facing direction.
            small_step_face_left = not bool(small_step_left_first)
        self.small_step_face_left = bool(small_step_face_left)
        # UI keeps this aligned with the configured fixed-attack key.  The
        # atomic 小碎步 owns two explicit taps between its two directions.
        self.small_step_attack_key = "ctrl"
        # Published from the movement loop.  Queued jump/buff/small-step
        # input is only allowed while a normal horizontal patrol or rope
        # approach decision is live.
        self._motion_arbiter_stage: Optional[str] = None
        # 站桩攻击 is deliberately independent of recorded patrol layers. The
        # UI enables it before Start Patrol.  The Start Patrol capture writes
        # a temporary current-position anchor; recorded route data is never
        # used by this mode.  When the temporary point matches a correctly
        # configured recorded route, it borrows that route to climb back after
        # a fall.  It never writes an anchor into the map.
        self.stationary_attack_enabled = False
        # UI-only association of the temporary standing point with an
        # existing recorded layer.  It remains visible even when the route
        # configuration is not sufficient to arm a return.
        self._stationary_ui_anchor_layer: Optional[str] = None
        self._stationary_route_anchor_layer: Optional[str] = None
        self._stationary_return_route_ready = False
        self._stationary_route_validation_error = ""
        self.stationary_facing_direction = "right"
        # 双向 is a session-only cycle. It starts facing right and flips only
        # from settled stationary frames, never in the middle of a recovery.
        self._stationary_bilateral_target = "right"
        self._stationary_bilateral_frames = 0
        self._stationary_attack_anchor: Optional[Point] = None
        self.stationary_pickup_enabled = False
        self.stationary_pickup_interval_seconds = 900.0  # 15.0m
        self.stationary_pickup_jitter_seconds = 0.0
        self._stationary_pickup_next_at = float("inf")
        self._stationary_pickup_phase: Optional[str] = None
        # A pickup round gets one automatic retry after it falls off the
        # patrol route.  The second consecutive fall ends that round and the
        # ordinary interval starts a fresh one later.
        self._stationary_pickup_failures = 0
        self._stationary_pickup_retry_after_return = False
        self._stationary_pickup_return_active = False
        # One longer lateral step after a return climb confirms that the
        # character has actually detached from the rope top before normal
        # stand-still recovery or pickup retry resumes.
        self._stationary_return_dismount_direction: Optional[str] = None
        # The horizontal direction this worker last COMMANDED in stand-still
        # mode - a recovery walk or an applied 朝向 tap.  The selected 朝向 is
        # owed whenever it differs from this value: a recovery walk turns the
        # character toward the anchor it walks to, so the field fault was a
        # character standing on its spot facing the way it came back from.
        # None means "unknown" (the operator's own positioning at Start Patrol
        # may face either way), which makes the selected side the one to apply.
        self._stationary_facing_command: Optional[str] = None
        # Records whether the last position was inside the anchor's X band (set
        # by the arrival branch, the 小碎步 pair and the 朝向 tap).  It is
        # deliberately NOT a gate on corrections any more: holding a "settled"
        # position left the character standing a pixel or two off the stake
        # instead of walking back, which the operator rejected in the field.
        self._stationary_x_settled = False
        self._stationary_near_correction_next_at = 0.0
        # A correction may aim at the facing turn point rather than the exact
        # anchor.  It is transient state for one queued arbiter step only.
        self._stationary_step_target_x: Optional[float] = None
        # Do not resume the independent attack cadence on the same frame as a
        # facing tap.  Fresh marker reads must first confirm the tap did not
        # nudge the character out of its accepted standing band.
        self._stationary_facing_confirm_frames_remaining = 0
        # Y recovery bookkeeping: how many jumps this displacement episode has
        # already spent and when the last one was sent.
        self._stationary_y_jumps = 0
        self._stationary_y_jump_at = float("-inf")
        self._stationary_y_backoff_logged = False
        self._stationary_y_mismatch_sign = 0
        self._stationary_y_mismatch_frames = 0
        self._walk_hold_key: Optional[str] = None
        self._walk_hold_z = False
        self._walk_hold_until = 0.0
        # Rope-approach stall recovery: when the character ends up ON the
        # rope mid-height, the walk toward the rope never advances X - the
        # worker must NOT keep pressing left/right+Z forever.  Track the
        # approach X and, once stalled while aligned with the rope, switch to
        # an attached climb (Up, no Z).
        self._rope_approach_last_x: Optional[float] = None
        self._rope_approach_stall_frames = 0
        self._rope_approach_far_stall_frames = 0
        self._rope_approach_far_stall_count = 0
        self._rope_approach_phase_label: Optional[str] = None
        self._rope_stuck_recoveries = 0
        # Locked rope target X of the current rope phase (label, x) and the
        # last logged climb direction, so per-frame projection jitter cannot
        # move the walk target or flip the lateral jump side.
        self._held_rope_target: Optional[tuple[str, float]] = None
        self._climb_direction_log: Optional[tuple[str, str]] = None
        # 自救：巡逻 5 分钟一检；若角色在小地图上连续 20 帧位置不变
        # （卡在角落/绳上），自动回到第一层并重启巡逻。
        self.rescue_check_interval_seconds = max(
            30.0, float(rescue_check_interval_seconds)
        )
        self.rescue_stuck_frames = max(5, int(rescue_stuck_frames))
        self._rescue_last_check = time.monotonic()
        self._rescue_last_pos: Optional[Point] = None
        self._rescue_stuck_frames = 0
        self._rescue_max_stuck = 0
        # Keep transient off-layer readings separate from the ordinary stuck
        # run. A single layer-band miss must never inherit earlier slow-walk
        # frames and launch the destructive Alt+Down rescue.
        self._rescue_off_route_frames = 0
        self._rescue_off_route_anchor: Optional[Point] = None
        self._rescue_active = False
        self.rescue_cycle_limit = max(1, int(rescue_cycle_limit))
        self._rescue_cycles = 0
        self.rescue_probe_attack_block_seconds = max(
            0.5, float(rescue_probe_attack_block_seconds)
        )
        self.rescue_probe_hold_seconds = max(
            0.1, float(rescue_probe_hold_seconds)
        )
        self.rescue_probe_settle_seconds = max(
            0.0, float(rescue_probe_settle_seconds)
        )
        self.rescue_probe_move_threshold = max(
            0.002, float(rescue_probe_move_threshold)
        )
        # Cross-process attack coordination: when the YOLO attack worker
        # reports an active target, patrol movement pauses (attack priority).
        self._attack_state = (
            AttackStateFile(attack_state_path)
            if attack_state_path else None
        )
        self._attack_paused_last = False
        # The attack keeps priority over patrol movement for a BOUNDED
        # window; past it the patrol pushes through and keeps walking, so a
        # stuck/unreachable target cannot freeze the patrol forever (e.g.
        # after a monster knock-down the character would stop at every move).
        self.attack_block_max_seconds = max(0.5, float(attack_block_max_seconds))
        self._attack_active_since: Optional[float] = None        # YOLO rope state: gates the inner-gap jump on the real screen gap.
        # State-independent floor verifier. Normal reconciliation runs every
        # frame, but an obsolete climb/drop phase can deliberately reject a
        # lower-floor marker. Recheck the already-computed marker at a modest
        # fixed cadence; this performs no capture and no second image scan.
        self._floor_verify_interval_seconds = 0.75
        self._last_floor_verify_at = float("-inf")
        self._floor_verify_candidate: Optional[str] = None
        self._floor_verify_frames = 0
        # rope_jump_px = max |screen gap| that still counts as "at the rope".
        self._rope_state = (
            RopeStateFile(rope_state_path)
            if rope_state_path else None
        )
        # Whether the YOLO detection subprocess owns the jump-rope logic.
        # Fixed Attack mode runs WITHOUT YOLO, so the rope jump must use the
        # minimap logic only (no fresh screen gap to consult).  Switched live
        # from the UI when the attack mode changes.
        self._yolo_detection_active = bool(yolo_detection_active)
        # Other-player safety net: when red diamonds (other players) show on
        # the minimap, switch channel automatically.  The scan is time-anchored
        # (every ``other_player_check_interval_seconds``, default 60 s) instead
        # of on every patrol cycle, so the minimap pixel check costs almost
        # nothing.  Switched live from the UI.
        self._other_player_check_enabled = bool(other_player_check_enabled)
        self.other_player_check_interval_seconds = max(
            1.0, float(other_player_check_interval_seconds)
        )
        self._last_other_player_check = float("-inf")
        self._player_switch_active = False
        self.other_player_drug_taps = max(1, int(other_player_drug_taps))
        self.other_player_drug_gap_seconds = max(
            0.1, float(other_player_drug_gap_seconds)
        )
        # Before each switch, an HP drug is eaten only when the current HP
        # ratio is below this threshold (default 70%).
        self.other_player_hp_threshold = max(
            0.0, min(1.0, float(other_player_hp_threshold))
        )
        # After a switch the new channel is re-checked for other players;
        # the switch repeats while any show up, up to this many attempts.
        self.other_player_switch_max_attempts = max(
            1, int(other_player_switch_max_attempts)
        )
        self.other_player_switch_settle_seconds = max(
            0.0, float(other_player_switch_settle_seconds)
        )
        self._other_player_request_message = str(other_player_request_message).strip()
        self._other_player_room_code = str(other_player_room_code or "").strip()[:128]
        self._other_player_wait_minutes = max(1.0, min(20.0, float(other_player_wait_minutes)))
        self._current_channel = normalize_channel(current_channel)
        self._on_channel_landed = on_channel_landed
        self._other_player_settings_lock = threading.RLock()
        # Shared state paths (overridable for tests): the StatusWorker's
        # HP/MP state file and the Drug panel's settings file.
        self.status_state_path = str(status_state_path) if status_state_path else str(
            Path(__file__).resolve().parent / "work" / "status_state.json"
        )
        self.drug_settings_path = (
            drug_settings_path if drug_settings_path
            else config_section_file("drug")
        )
        self._last_frame: Any = None
        self._last_minimap_region: Any = None
        # Cross-process patrol state: published so the YOLO attack worker
        # blocks attacks while the character is climbing/dropping.
        self._patrol_state = (
            PatrolStateFile(patrol_state_path)
            if patrol_state_path else None
        )
        # Busy hysteresis: keep patrol_state busy for this many seconds after
        # the last climb/drop frame, so brief idle resets between climb
        # attempts cannot unblock the attack mid-rope.
        self._patrol_busy_hold = max(0.5, float(patrol_busy_hold))
        self._patrol_busy_until = 0.0
        # Last horizontal direction patrol moved the character (left/right),
        # published so the YOLO attack worker can sync its facing belief.
        self._patrol_facing: Optional[str] = None
        self.rope_jump_px = max(20.0, float(rope_jump_px))
        # When the character's screen X is within this many pixels of the
        # rope, it is considered ON the rope: YOLO stops jumping and patrol's
        # climb state machine holds Up.  This dead zone prevents YOLO from
        # flipping the jump direction as the character passes the rope X and
        # yanking it off the rope mid-climb.
        self.on_rope_px = max(10.0, min(float(on_rope_px), self.rope_jump_px))
        # |screen gap| at which the character counts as directly UNDER the
        # rope: jump straight up (Alt+Up) instead of a left/right chord, so
        # the sideways jump cannot shove the character past the rope.  The
        # default 10px is a tight center band right around the rope line.
        self.under_rope_px = max(2.0, min(float(under_rope_px), self.on_rope_px))
        # MOVE-TO-ROPE tap length while the YOLO screen gap is fresh: short
        # taps re-check the gap every frame so the character creeps into the
        # jump window instead of overshooting past the rope.
        self.rope_approach_creep_seconds = max(
            0.05, float(rope_approach_creep_seconds)
        )
        # Tiny random step bounds used while creeping inside the honey zone
        # (near-rope band): short, human-like jitter instead of fixed taps.
        self.rope_tiny_step_min_seconds = max(
            0.01, float(rope_tiny_step_min_seconds)
        )
        self.rope_tiny_step_max_seconds = max(
            self.rope_tiny_step_min_seconds, float(rope_tiny_step_max_seconds)
        )
        self._last_minimap_box: Optional[tuple[int, int, int, int]] = None
        self._last_structure_mode: Optional[str] = None
        self._debug_last_layer: Optional[str] = None
        self._dispatched_position_logged: Optional[float] = None
        self._last_drop_attempt = float("-inf")
        # The edge-walk fallback is deliberately scoped to an automatic
        # reconnect start.  Ordinary Start Patrol and ordinary fall recovery
        # continue to use their existing drop behaviour unchanged.
        self._reconnect_drop_recovery_armed = False
        self._reconnect_drop_attempt_y: Optional[float] = None
        self._reconnect_drop_attempt_at = float("-inf")
        self._reconnect_drop_assessed_at = float("-inf")
        self._reconnect_drop_stalled_attempts = 0
        self._reconnect_drop_edge_phase: Optional[str] = None
        self._reconnect_drop_edge_started_at = 0.0
        self._reconnect_drop_edge_start_y: Optional[float] = None
        # Drop-to-route landing evidence (see ``_drop_landing_floor``): where
        # the descent started, how far it has really moved the marker, and the
        # settled confirmation of a floor whose band cannot contain the reading.
        self._drop_entry_y: Optional[float] = None
        self._drop_lowest_y: Optional[float] = None
        self._drop_last_y: Optional[float] = None
        self._drop_settled = False
        self._drop_arrival_candidate: Optional[str] = None
        self._drop_arrival_frames = 0
        # The relaxed landing answer for the marker Y it was resolved at, so the
        # shared fallbacks log their evidence once per reading.
        self._drop_relaxed_y: Optional[float] = None
        self._drop_relaxed_floor: Optional[str] = None
        self.last_observation: Optional[MinimapObservation] = None
        self.last_decision: Optional[MovementDecision] = None
        self._last_send = 0.0
        self._aligned_frames = 0
        self._last_climb_attempt = float("-inf")
        self._climb_state = ClimbState()
        self._rope_approach_direction: Optional[str] = None
        # Set while the character is dropping from the final layer all the
        # way down to the first layer.  While descending, layer resync is
        # suppressed so an intermediate platform cannot hijack the descent
        # and restart patrol on a middle layer.
        self._descending_to_first = False
        # The floor the descent last saw the marker on while it kept the route (the log evidence, once
        # per floor instead of every frame).
        self._drop_descent_saw: Optional[str] = None
        # When the planned descent started, for DROP_TO_FIRST_MAX_SECONDS.
        self._descending_since: Optional[float] = None
        # ---------- FALLING RECOVERY + RETURN TO ROUTE ----------
        # ``_track_fall`` counts consecutive frames where the diamond Y drops
        # fast (an unexpected fall - knocked down, missed a stair, walked off
        # an edge).  It is suppressed while the intentional drop-to-layer1
        # descent or a rope climb is active, so those never get interrupted.
        # When the fall stops, the floor is re-detected: in-range floors
        # restart patrol there; floors outside the contiguous patrol range
        # start ``_return_mode`` (climb back up / drop back down, attacks
        # paused) until the range is reached again.
        self._fall_detect_frames = max(2, int(fall_detect_frames))
        self._fall_marker_y_gain = max(0.005, float(fall_marker_y_gain))
        self._fall_last_y: Optional[float] = None
        self._fall_frames = 0
        self._fall_pending = False
        # While the marker is descending, the world tracker can name a real
        # lower floor for one frame and then lose it when the minimap scrolls.
        # Keep that evidence until the fall settles; otherwise the next blank
        # frame leaves the old route state (for example layer3) active.
        self._fall_floor_candidate: Optional[str] = None
        # Set once per confirmed fall: every movement key was released (see
        # ``_release_stuck_keys``) so a key whose key-up the game missed
        # during the knock-down cannot pin the character against a wall after
        # landing.
        self._fall_keys_released = False
        # Landing-reconciliation settle state (see _reconcile_landed_floor):
        # confident world-Y samples collected after a fall stops, until they
        # stabilize or the settle window times out.
        self.fall_settle_min_frames = max(2, int(fall_settle_min_frames))
        self.fall_settle_epsilon = max(0.02, float(fall_settle_epsilon))
        self.fall_settle_max_seconds = max(
            0.3, float(fall_settle_max_seconds)
        )
        self._fall_settle_started: Optional[float] = None
        self._fall_settle_world: list[float] = []
        # World-Y drift watchdog tuning (see _world_drift_check):
        self.world_drift_check_interval_seconds = max(
            0.5, float(world_drift_check_interval_seconds)
        )
        self.world_drift_reanchor_threshold = max(
            0.05, float(world_drift_reanchor_threshold)
        )
        self._last_world_drift_check = 0.0
        self._last_fall_resolved_at: Optional[float] = None
        # Raw (pre-pin) world-Y reading of the current frame, captured where
        # the structure tracker result is merged so reconciliation and the
        # drift watchdog never see the pinned/aliased value.
        self._raw_world_y: Optional[float] = None
        self._raw_structure_confidence = 0.0
        self._return_mode: Optional[str] = None  # "climb-to-route" | "drop-to-route"
        # Floor the return started from: while climbing back, a failed
        # grab falls the marker back to a Y between every recorded band;
        # keep targeting this floor's own rope instead of waiting forever.
        self._return_from_floor: Optional[str] = None
        # Stable in-range floor detected at the top of a return climb. Return
        # climbs use the normal frame confirmation and rope-top compensation
        # before patrol is allowed to resume.
        self._return_arrival_floor: Optional[str] = None
        # A normal (non-climb, non-fall) layer transition must survive several
        # fresh minimap observations before it is allowed to restart patrol on
        # another layer.  A single bad yellow-marker frame otherwise changes
        # the route endpoint and immediately reverses Left/Right.  Confirmed
        # falls and rope arrivals have their own stronger state machines and
        # deliberately bypass this cruising-only debounce.
        self._layer_resync_candidate: Optional[str] = None
        self._layer_resync_candidate_frames = 0
        self._normal_layer_resync_frames = 4
        # Stair jump: during the left-most/right-most patrol walk the worker
        # detects when the marker stops advancing while a walk hold is being
        # issued (a stair blocks the walk) and jumps - holding the travel
        # direction and tapping Alt - WITHOUT any recorded jump points.
        self.stair_jump_enabled = bool(stair_jump_enabled)
        self.stair_jump_stall_diamonds = max(0.01, float(stair_jump_stall_diamonds))
        self.stair_jump_stall_frames = max(1, int(stair_jump_stall_frames))
        # After Start Patrol the character stands still for a moment - that is
        # NOT being stuck at a stair, and it should not jump onto a rope it
        # happens to start next to.  Jumps (stair jump and rope jump_climb)
        # are suppressed for this many seconds after the patrol (re)starts.
        self.patrol_start_grace_seconds = max(
            0.0, float(patrol_start_grace_seconds)
        )
        self._patrol_started_at: Optional[float] = None
        self.stair_jump_attempts_max = max(1, int(stair_jump_attempts_max))
        self.stair_jump_grace_seconds = max(0.0, float(stair_jump_grace_seconds))
        self.stair_jump_alt_hold_seconds = max(
            0.01, float(stair_jump_alt_hold_seconds)
        )
        self.stair_jump_lead_seconds = max(0.0, float(stair_jump_lead_seconds))
        # After a rope climb reaches the next layer the character is still
        # settling on the platform edge - a stalled walk there is not a stair
        # and jumping left/right can drop it off the platform.  No stair jump
        # for this many seconds after the climb arrival.
        self.stair_jump_climb_arrival_grace_seconds = max(
            0.0, float(stair_jump_climb_arrival_grace_seconds)
        )
        self._climb_arrival_at: Optional[float] = None
        # Current-analysis-unit stall threshold (set per frame from the
        # diamond-relative setting once a CoordinateLayout is known).
        self._current_stair_jump_stall = STAIR_JUMP_STALL_FALLBACK
        self._reset_stair_state()

    def _reset_stair_state(self) -> None:
        """Clear the per-approach stair-jump tracking state."""

        self._stair_state = {
            "phase_label": None,   # e.g. "layer1.right-most"; reset on change
            # The last N marker positions.  Stair recovery uses a sliding
            # window instead of one unbroken run: minimap jitter or one
            # moving frame must not turn a genuinely frozen character into a
            # fresh sequence that can queue another jump.
            "position_window": [],
            "stall_frames": 0,     # frozen X+Y transitions in that window
        }

    def _on_stair_jump_complete(self, succeeded: bool) -> None:
        """Receive the dedicated worker result without mutating state there."""

        with self._stair_jump_completion_lock:
            self._stair_jump_completion = bool(succeeded)

    def set_stair_jump_worker(self, worker: Any) -> None:
        """Attach the dedicated stair-jump executor after construction."""

        self.stair_jump_worker = worker

    def arm_patrol_input(self) -> None:
        """Permit a fresh patrol session after its sender has been reset."""

        self._patrol_abort_event.clear()

    def disarm_patrol_input(self) -> None:
        """Immediately invalidate delayed movement without sending a key.

        The central sender owns the one authoritative game-side key scrub.
        This method deliberately only cancels worker-local work: it is safe
        to call from the UI thread even when a movement transaction holds the
        directional locks, and it cannot inject a late key-up over the
        operator's manual controls.
        """

        self._patrol_abort_event.set()
        if self.direction_transition_event is not None:
            self.direction_transition_event.clear()
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.stationary_recovery_active_event is not None:
            self.stationary_recovery_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.moving_active_event is not None:
            self.moving_active_event.clear()
        if self.pickup_active_event is not None:
            self.pickup_active_event.clear()

    def _patrol_input_allowed(self) -> bool:
        """Whether this worker may still finish the current motion."""

        return bool(
            not self.stop_event.is_set()
            and not self._patrol_abort_event.is_set()
            and (self.automation_active_event is None
                 or self.automation_active_event.is_set())
        )

    def _wait_for_patrol_motion(self, seconds: float) -> bool:
        """Wait only while the current patrol lifecycle remains valid."""

        if seconds <= 0:
            return self._patrol_input_allowed()
        deadline = time.monotonic() + float(seconds)
        while self._patrol_input_allowed():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._patrol_abort_event.wait(min(0.02, remaining)):
                return False
        return False

    def _consume_stair_jump_completion(self) -> None:
        """Apply the confirmed queue result on the movement thread.

        The detector quiet-period starts when a token is registered, not when
        it eventually executes.  A rejected token simply clears its local
        evidence so the next normal five-frame window can retry.
        """

        with self._stair_jump_completion_lock:
            completed = self._stair_jump_completion
            self._stair_jump_completion = None
        if completed is None:
            return
        if completed:
            LOG.info("STAIR JUMP executed")
            return
        # The pending action never reached the game. Rebuild its evidence
        # window so the next normal seven-sample freeze can retry.
        state = self._stair_state
        state["position_window"].clear()
        state["stall_frames"] = 0
        LOG.warning("STAIR JUMP did not execute; detector re-armed")

    @staticmethod
    def _is_walk_key(key: Optional[str]) -> bool:
        """True for plain direction holds and stair-jump walk-and-hop keys."""

        return key in ("left", "right") or (
            isinstance(key, str) and key.startswith("stair_jump_")
        )

    @staticmethod
    def _patrol_facing_for_key(key: Optional[str]) -> Optional[str]:
        """Horizontal facing implied by a movement decision.

        Returns the last direction the character was moved (for the attack
        worker to sync its facing belief), or None for non-directional keys
        (no-op "wait" decisions during a climb, aligned, etc.).  Must never
        raise on a None key.
        """

        if key in ("left", "right"):
            return key
        if isinstance(key, str) and key.startswith("stair_jump_"):
            return key.removeprefix("stair_jump_")
        if key in ("jump_climb_left", "jump_climb_right"):
            return key.removeprefix("jump_climb_")
        return None

    def _update_moving_event(self, decision: MovementDecision) -> None:
        """Drive the walking-state event used to gate Z pickup."""

        if self.moving_active_event is None:
            return
        if self._is_walk_key(decision.key):
            if not self.moving_active_event.is_set():
                LOG.debug("moving: pickup Z enabled")
            self.moving_active_event.set()
        else:
            if self.moving_active_event.is_set():
                LOG.debug("not moving: pickup Z paused")
            self.moving_active_event.clear()

    def _stair_jump_decision(
        self,
        observation: MinimapObservation,
        route_label: str,
        position_plan: Optional[PositionMovementPlan],
        now: float,
    ) -> Optional[MovementDecision]:
        """Return a stair-jump decision when walking is blocked by a stair.

        Runs only in the move-to-left-most / move-to-right-most phases.  A
        seven-position sliding window has six transitions; when all six are
        frozen in *both* X and Y, a stair blocks the walk and the worker emits
        one ``stair_jump_<direction>`` action.  No jump points need to be
        recorded - any impassable stair is jumped automatically.  The action
        is then locked for the rest of that patrol leg.
        """

        if (not self.stair_jump_enabled or observation.player is None
                or observation.confidence < self.minimum_confidence
                or position_plan is None
                or position_plan.reached_or_crossed
                or position_plan.decision.key not in ("left", "right")):
            return None
        # Patrol-start grace: the character standing still right after Start
        # Patrol is not stuck at a stair - no jump until it has moved.
        started = self._patrol_started_at
        if (started is not None
                and now < started + self.patrol_start_grace_seconds):
            return None
        # Climb-arrival grace: right after a rope climb reached the next
        # layer the character is still on/near the platform edge - a stalled
        # walk there is not a stair, and jumping can drop it off the
        # platform.  No stair jump until it has moved for a moment.
        climb_arrival = getattr(self, "_climb_arrival_at", None)
        if (climb_arrival is not None
                and now < climb_arrival
                + self.stair_jump_climb_arrival_grace_seconds):
            return None
        if route_label.endswith(".left-most"):
            direction = "left"
        elif route_label.endswith(".right-most"):
            direction = "right"
        else:
            return None
        # A registered stair event owns the next five detection frames. The
        # counter lives outside per-route state, so a transient left/right
        # route reset cannot produce four immediate re-registrations.
        if self._stair_jump_skip_frames > 0:
            self._stair_jump_skip_frames -= 1
            return None
        state = self._stair_state
        if state["phase_label"] != route_label:
            self._reset_stair_state()
            state = self._stair_state
            state["phase_label"] = route_label
        px = observation.player.x
        window = state["position_window"]
        # Fixed attacks may create minor marker jitter or briefly suppress a
        # walk hold. They must count as frozen time for stair recovery rather
        # than breaking the evidence window. Use the previous sample while
        # the arbiter reports an attack animation; the queued stair action
        # itself still waits for that attack to finish before pressing Alt.
        attack_motion_active = getattr(
            self.motion_arbiter, "attack_motion_active", None
        )
        if (window and callable(attack_motion_active)
                and attack_motion_active()):
            window.append(window[-1])
        else:
            window.append((px, observation.player.y))
        window_size = max(2, self.stair_jump_stall_frames)
        if len(window) > window_size:
            del window[:-window_size]
        if len(window) < window_size:
            state["stall_frames"] = 0
            return None
        threshold = self._current_stair_jump_stall
        frozen_steps = sum(
            1
            for previous, current in zip(window, window[1:])
            if (abs(current[0] - previous[0]) < threshold
                and abs(current[1] - previous[1]) < threshold)
        )
        state["stall_frames"] = frozen_steps
        net_frozen = (
            abs(window[-1][0] - window[0][0]) < threshold
            and abs(window[-1][1] - window[0][1]) < threshold
        )
        # With the default seven-position window there are six transitions;
        # all six must be frozen in both axes.  A one-frame slide therefore
        # cannot restart a separate consecutive-frame counter.  The net
        # movement guard keeps normal slow walking (small but accumulating
        # steps) from being mistaken for a stair.
        if frozen_steps < window_size - 1 or not net_frozen:
            return None
        LOG.info(
            "STAIR JUMP %s at x=%.6f after %d frozen X/Y transitions",
            direction, px, frozen_steps,
        )
        return MovementDecision(
            f"stair_jump_{direction}",
            f"stuck at stair x={px:.6f}; jump while walking",
            self.movement_hold_seconds,
        )

    def _send_stair_jump(self, decision: MovementDecision) -> bool:
        """Hold the travel direction and tap Alt (jump) mid-hold.

        A short direction-only lead gives the character forward momentum
        before the Alt tap - a standing jump does not carry over the stair.
        Mirrors ``_send_walk_hold``: the direction key is released early
        (within ~20ms) when the YOLO attack takes over, so a mob can
        interrupt the jump approach like any other walk.
        """

        direction = decision.key.removeprefix("stair_jump_")
        if direction not in ("left", "right"):
            return False
        if not _sender_is_safe(self.key_sender):
            LOG.warning("movement suppressed: target window is not safely selected")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            LOG.warning("stair jump requires key_down() and key_up(); suppressed")
            return False
        with self._direction_lock:
            claimed = key_down(direction) is not False
            if not claimed:
                return False
            deadline = time.monotonic() + max(0.01, float(decision.duration))
            alt_down = False
            try:
                lead = min(
                    max(0.0, self.stair_jump_lead_seconds),
                    max(0.01, float(decision.duration) * 0.5),
                )
                if lead > 0:
                    time.sleep(lead)
                if key_down("alt") is not False:
                    alt_down = True
                    time.sleep(max(0.01, self.stair_jump_alt_hold_seconds))
                    # Alt is the jump *tap*, not the movement hold. Keeping
                    # it down through the direction hold makes the game
                    # auto-repeat jumps from one stair event.
                    key_up("alt")
                    alt_down = False
                # 跳一旦开始就让它完成：中途被攻击打断会让角色卡在坑/边缘
                # （跳跃被压制 → 超过 2 秒不动）。攻击等待这一跳。
                while time.monotonic() < deadline:
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.02)
                return True
            finally:
                if alt_down:
                    key_up("alt")
                key_up(direction)

    def perform_stair_jump(self, direction: str, hold_up: bool = False) -> bool:
        """Add one Alt tap to an already-running patrol direction.

        The patrol walk remains owned by ``_send_walk_hold`` while the
        dedicated worker waits for an attack animation.  Do not scrub all
        movement keys here: that was the source of the visible pause after an
        attack completed.  This method takes a short, additional direction
        claim only when patrol no longer owns it, then releases only that
        claim after the Alt tap.
        """

        direction = str(direction).casefold()
        if (not self._patrol_input_allowed()
                or direction not in ("left", "right")
                or not self.patrol_enabled):
            return False
        if not _sender_is_safe(self.key_sender):
            LOG.warning("stair jump suppressed: target window is not safely selected")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            LOG.warning("stair jump requires key_down() and key_up(); suppressed")
            return False
        with self._direction_lock:
            # Any new jump replaces a previous jump-point rope attempt.  A
            # stale Up must never survive into the next Alt jump.
            if self._jump_point_up_held:
                key_up("up")
                self._jump_point_up_held = False
                self._jump_point_up_started_at = 0.0
                self._jump_point_y_samples.clear()
                self._jump_point_start_y = None
                self._jump_point_last_y = None
                self._jump_point_rise_frames = 0
                self._jump_point_rope_handoff = False
                if self.climbing_active_event is not None:
                    self.climbing_active_event.clear()
                LOG.info("JUMP POINT: released prior Up hold for new jump")
            if not self._patrol_input_allowed():
                return False
            is_key_down = getattr(self.key_sender, "is_key_down", None)
            walk_is_held = bool(
                self._walk_hold_key == direction
                and (not callable(is_key_down) or is_key_down(direction))
            )
            added_direction_claim = False
            if not walk_is_held:
                added_direction_claim = key_down(direction) is not False
                if not added_direction_claim:
                    return False
            alt_down = False
            jump_point_up_started = False
            jump_succeeded = False
            try:
                # A recorded point has already met its exact X/Y condition.
                # For a rope, Up must be down *before* the jump frame rather
                # than 150ms later, otherwise the character can pass the rope
                # before the game sees the climb input.
                if hold_up:
                    if key_down("up") is False:
                        return False
                    jump_point_up_started = True
                    self._jump_point_up_held = True
                    self._jump_point_up_started_at = time.monotonic()
                    self._jump_point_y_samples.clear()
                    self._jump_point_start_y = None
                    self._jump_point_last_y = None
                    self._jump_point_rise_frames = 0
                    if self.climbing_active_event is not None:
                        self.climbing_active_event.set()
                lead = 0.0 if hold_up else max(0.0, self.stair_jump_lead_seconds)
                if lead and not self._wait_for_patrol_motion(lead):
                    return False
                if not self._patrol_input_allowed():
                    return False
                if key_down("alt") is False:
                    return False
                alt_down = True
                alt_hold = (
                    min(0.03, self.stair_jump_alt_hold_seconds)
                    if hold_up else self.stair_jump_alt_hold_seconds
                )
                if not self._wait_for_patrol_motion(alt_hold):
                    return False
                key_up("alt")
                alt_down = False
                if hold_up:
                    LOG.info("JUMP POINT executed: Up was held before Alt and remains held until landing Y settles")
                jump_succeeded = True
                return True
            finally:
                if alt_down:
                    key_up("alt")
                if jump_point_up_started and not jump_succeeded:
                    key_up("up")
                    self._jump_point_up_held = False
                    self._jump_point_up_started_at = 0.0
                    self._jump_point_y_samples.clear()
                    self._jump_point_start_y = None
                    self._jump_point_last_y = None
                    self._jump_point_rise_frames = 0
                    self._jump_point_rope_handoff = False
                    if self.climbing_active_event is not None:
                        self.climbing_active_event.clear()
                if added_direction_claim:
                    key_up(direction)

    def _jump_point_leg_key(self) -> tuple[int, int, str]:
        """Identity of the current movement leg: (pass, route floor, phase).

        A leg is one directional traversal of one route floor.  The patrol
        cycle is deliberately NOT part of the key: walking the same leg a
        second time is the same leg, and a recorded point must not fire twice
        on it (that is the repeated 右跳 of the field report).
        """

        index = -1 if self._route_layer_index is None else int(self._route_layer_index)
        return (int(self._jump_point_pass_id), index, str(self._route_phase))

    def _begin_jump_point_pass(self, reason: str) -> None:
        """Start a new movement pass: every recorded jump point is armed again.

        A pass is one Start Patrol session (or one 站桩/pickup circuit).  The
        operator's rule: points are allowed once per movement pass, never once
        per application lifetime, so a point that fired on the first run is
        available on the next Start Patrol.
        """

        self._jump_point_pass_id += 1
        self._jump_point_fired.clear()
        LOG.info(
            "JUMP POINT pass %d started (%.3fs): recorded points re-armed (%s)",
            self._jump_point_pass_id,
            time.monotonic(),
            reason,
        )

    def _jump_point_block_reason(
        self, token: tuple[str, int], *, climbing: bool
    ) -> Optional[str]:
        """Why ``token`` must not fire now, or None when it is armed."""

        record = self._jump_point_fired.get(token)
        if record is None:
            return None
        pass_id, leg_key, fired_at = record
        if climbing and pass_id == self._jump_point_pass_id:
            # The point already produced its jump in this pass.  Re-firing it
            # from a climb is the "already on the rope and jumped again" case:
            # a mount point recorded on the floor row still matches while the
            # climb is only a few pixels up.
            return "already used in this pass (climb)"
        if leg_key == self._jump_point_leg_key():
            return "already fired on this leg"
        cooldown = JUMP_POINT_REFIRE_COOLDOWN_SECONDS
        if time.monotonic() - fired_at < cooldown:
            return f"within the {cooldown:.1f}s refire cooldown"
        return None

    def _jump_point_note_fired(self, token: tuple[str, int]) -> None:
        """Record a dispatched point for its leg/pass and cooldown."""

        self._jump_point_fired[token] = (
            int(self._jump_point_pass_id),
            self._jump_point_leg_key(),
            time.monotonic(),
        )

    def _jump_point_decision(
        self,
        observation: MinimapObservation,
        layer: Optional[str],
        plan: Optional[PositionMovementPlan],
        *,
        climbing: bool = False,
        travel_direction: Optional[str] = None,
    ) -> Optional[MovementDecision]:
        """Return a jump when the marker enters an armed recorded point.

        The point layer is not a gate: rope approach frames can have an
        ambiguous/current layer label.  The recorded *direction* is always a
        gate: 右跳 fires only while travelling right and 左跳 only while
        travelling left, including during a rope approach.  A climb with no
        known horizontal approach direction does not guess.  Allowing either
        point during a climb made a 左跳 fire while the route phase was right,
        which then left the rope planner in a misleading ``action=wait`` state.

        A point fires at most ONCE PER LEG (``_jump_point_leg_key``) and is
        armed again by the next leg or the next Start Patrol pass.  Leaving the
        zone is not enough to re-arm it: the zone is loose on purpose, so a
        landing, a walk back over the same spot, or a climb past the same row
        re-enters it immediately, which produced a second 右跳 while the
        character was already on the rope.
        """

        del layer
        player = observation.player
        if player is None:
            self._jump_point_inside_tokens.clear()
            return None
        # A falling marker can cross an unrelated point's exact X/Y window.
        # It is not standing at that point and must not dispatch a directional
        # Alt jump that suppresses the landing resolver.  Let the fall settle
        # first; the next patrol leg will re-arm legitimate points.
        if self._fall_frames > 0 or self._fall_pending:
            return None
        if travel_direction not in ("left", "right"):
            travel_direction = (
                plan.decision.key
                if plan is not None and plan.decision.key in ("left", "right")
                else None
            )
        # A directional point must never guess at the rope centre.  The
        # approach direction is retained by the route/climb state, so if it
        # is absent this frame simply cannot dispatch a left/right record.
        if travel_direction not in ("left", "right"):
            return None

        matches: list[tuple[tuple[str, int], str, str]] = []
        for point_layer, point_layer_data in self.important_positions.items():
            if not isinstance(point_layer_data, dict):
                continue
            points = point_layer_data.get("jump_points", [])
            if not isinstance(points, list):
                continue
            for index, point in enumerate(points):
                if not isinstance(point, dict):
                    continue
                direction = str(point.get("direction", "")).casefold()
                if direction not in ("left", "right"):
                    continue
                if direction != travel_direction:
                    continue
                try:
                    matched = (
                        abs(player.x - float(point["x"])) <= JUMP_POINT_X_TOLERANCE
                        and abs(player.y - float(point["y"])) <= JUMP_POINT_Y_TOLERANCE
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if matched:
                    matches.append(((point_layer, index), point_layer, direction))

        if climbing and len(matches) > 1:
            # Two points can match at once on a rope (a 左跳 and a 右跳 recorded
            # at the same height).  Prefer the recorded direction the character
            # is actually travelling; the stable sort keeps the X-sorted
            # recording order for everything else.
            matches.sort(key=lambda match: match[2] != travel_direction)

        matched_tokens = {token for token, _name, _direction in matches}
        # A fired point stays suppressed until the marker leaves its zone (the
        # Up-hold/landing session may still be open).  Re-arming on zone exit is
        # kept for the per-frame entry detector only; the leg record above is
        # what limits a point to one jump per leg.
        if not self._jump_point_up_held:
            self._jump_point_suppressed_tokens.intersection_update(matched_tokens)
        entered = [
            match for match in matches
            if (match[0] not in self._jump_point_inside_tokens
                and match[0] not in self._jump_point_suppressed_tokens)
        ]
        armed_entered: list[tuple[tuple[str, int], str, str]] = []
        for match in entered:
            reason = self._jump_point_block_reason(match[0], climbing=climbing)
            if reason is None:
                armed_entered.append(match)
                continue
            # One line per crossing is the point of the change: the operator has
            # to be able to see that the guard is what stopped the second jump.
            LOG.info(
                "JUMP POINT suppressed: %s[%d] %s jump - %s",
                match[1], match[0][1], match[2], reason,
            )
        self._jump_point_inside_tokens = matched_tokens
        if not armed_entered:
            return None

        token, point_layer, direction = armed_entered[0]
        self._jump_point_candidate = token
        LOG.info(
            "%s JUMP POINT %s[%d] entered x=%.6f y=%.6f; awaiting jump worker",
            "LEFT" if direction == "left" else "RIGHT",
            point_layer, token[1], player.x, player.y,
        )
        return MovementDecision(
            f"jump_point_{direction}", "recorded jump point",
            self.minimum_final_hold_seconds,
        )

    def _handoff_jump_point_rope_climb(
        self, observation: MinimapObservation
    ) -> None:
        """Hand a confirmed point-to-rope grab to ordinary Up-only climbing.

        The recorded point owns the first ``direction + Alt`` chord.  After
        the first genuine upward marker advance hands control to ordinary
        climb tracking; that state then performs the stricter attachment
        verification. From that moment it owns the *existing* Up hold so
        it can recognise the next floor.  It must not issue a second Alt jump.
        If the marker then falls back, the ordinary MOVE TO ROPE recovery is
        free to retry normally.
        """

        player = observation.player
        if player is None or not self._jump_point_up_held:
            return
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if not callable(key_down) or not callable(key_up):
            return
        with self._direction_lock:
            # Add the climb state's ownership claim before releasing the
            # point's claim, so the physical Up key never flickers up.
            if key_down("up") is False:
                return
            key_up("up")
        world_y = (
            observation.world_y_diamonds
            if observation.structure_confidence >= 0.12 else None
        )
        self._climb_state = ClimbState(
            phase="climbing-up",
            baseline_y=self._jump_point_start_y,
            baseline_world_y=world_y,
            up_held=True,
            attach_frames=2,
            last_world_y=world_y,
            last_marker_y=player.y,
            recent_y=[player.y],
        )
        self._jump_point_up_held = False
        self._jump_point_up_started_at = 0.0
        self._jump_point_y_samples.clear()
        self._jump_point_start_y = None
        self._jump_point_last_y = None
        self._jump_point_rise_frames = 0
        self._jump_point_rope_handoff = False
        if self.climbing_active_event is not None:
            self.climbing_active_event.set()
        LOG.info(
            "JUMP POINT rope grab confirmed; handed existing Up hold to normal climb tracking"
        )

    def _update_jump_point_landing(self, observation: MinimapObservation) -> None:
        if not self._jump_point_up_held or observation.player is None:
            return
        current_y = float(observation.player.y)
        if self._jump_point_start_y is None:
            # Keep the first capture after the recorded directional jump.  It
            # is the only reliable baseline for distinguishing a real platform
            # landing from a failed jump that simply settles back where it
            # started.
            self._jump_point_start_y = current_y
        marker_rise = (
            self._jump_point_last_y is not None
            and self._jump_point_last_y - current_y >= 0.003
        )
        self._jump_point_last_y = current_y
        if marker_rise:
            self._jump_point_rise_frames += 1
        elif self._jump_point_rise_frames:
            # A rebound to the departing floor restarts the provisional
            # evidence before ordinary climb tracking has accepted it.
            self._jump_point_rise_frames = 0
        if (self._jump_point_rope_handoff
                and self._jump_point_rise_frames >= 1):
            self._handoff_jump_point_rope_climb(observation)
            return
        # The samples immediately after Alt can still be from the take-off
        # frame.  Keep Up down through that jump session, then accept only a
        # later *higher* stable Y sequence as a landing on a horizontal
        # platform.  A stable departure-floor reading must not cause an early
        # Up release; otherwise the ordinary rope planner takes over with
        # ``jump_climb_up`` or an Alt+left/right recovery, overwriting the
        # recorded left/right jump point.
        if (time.monotonic() - self._jump_point_up_started_at
                < JUMP_POINT_LANDING_GUARD_SECONDS):
            return
        self._jump_point_y_samples.append(current_y)
        if len(self._jump_point_y_samples) > JUMP_POINT_LANDING_STABLE_FRAMES:
            del self._jump_point_y_samples[:-JUMP_POINT_LANDING_STABLE_FRAMES]
        layer = None
        if self._jump_point_candidate is not None:
            layer = self.important_positions.get(self._jump_point_candidate[0])
        marker_row = _layer_marker_row(layer) if layer is not None else None
        upward_landing_required = max(0.003, (marker_row or 0.007) * 0.5)
        settled_on_higher_floor = bool(
            self._jump_point_start_y is not None
            and self._jump_point_start_y - current_y >= upward_landing_required
        )
        if (len(self._jump_point_y_samples) == JUMP_POINT_LANDING_STABLE_FRAMES
                and max(self._jump_point_y_samples) - min(self._jump_point_y_samples) <= 0.001
                and settled_on_higher_floor):
            # End this jump-point session: release OUR claim on Up (one owner
            # reference) and stop sampling.  When the climb state machine still
            # holds its own reference the key physically stays down - which is
            # correct, the climb owns it.  In that case the climb's gate must
            # also stay set: clearing ``climbing_active_event`` here un-gated
            # attacks and 小碎步 while the character was still attached to the
            # rope (13:58 log: owners 2 -> 1 with "JUMP POINT landing Y settled"
            # while the climb state was holding-up-awaiting-progress).
            climb_owns_up = bool(
                self._climb_state.up_held
                or self._climb_state.phase == "climbing-up"
            )
            key_up = getattr(self.key_sender, "key_up", None)
            if callable(key_up):
                key_up("up")
            self._jump_point_up_held = False
            self._jump_point_up_started_at = 0.0
            self._jump_point_y_samples.clear()
            self._jump_point_start_y = None
            self._jump_point_last_y = None
            self._jump_point_rise_frames = 0
            self._jump_point_rope_handoff = False
            if climb_owns_up:
                LOG.info(
                    "JUMP POINT landing Y settled; released Up and left it to "
                    "the climb that still owns it"
                )
            else:
                if self.climbing_active_event is not None:
                    self.climbing_active_event.clear()
                LOG.info(
                    "JUMP POINT landing Y settled; released Up and resumed patrol"
                )

    def perform_queued_stair_jump(self, direction: str) -> bool:
        """Compatibility entry point for older integrations.

        Production now calls :meth:`perform_stair_jump` through the dedicated
        ``StairJumpWorker`` rather than the shared motion-arbiter queue.
        """

        return self.perform_stair_jump(direction)

    def _send_drop_through_platform(self) -> bool:
        """Emit Alt+Down while excluding patrol and 小碎步 directions."""

        with self._direction_lock:
            return _drop_through_platform(
                self.key_sender, self.drop_chord_hold_seconds
            )

    def _attack_should_defer(self) -> bool:
        """True when an active attack must wait for a rope climb, a drop, or
        a stuck-at-edge jump to finish.

        While the climb state machine owns the Up key (``up_held`` - grab
        attempt or attached climb) the attack waits: releasing Up mid-grab
        or mid-climb makes the character fall off the rope.  Same for a
        stair/pit-edge stall: the attack gate would otherwise pause the
        whole frame and suppress the stair-jump decision, leaving the
        character stuck at the edge for seconds.

        Returning to the patrol floor range (``_return_mode``) also defers
        the attack: the return climb/drop is protected exactly like a rope
        climb, and the return walk keeps the character moving instead of
        fighting mid-return.
        """

        if self._climb_state.up_held or (
                self.dropping_active_event is not None
                and self.dropping_active_event.is_set()):
            return True
        # Returning to the patrol floor range: attacks wait for the whole
        # return (climb/drop/walk) - the character must get back to the
        # patrol route before fighting again.
        if self._return_mode is not None:
            return True
        # A fully qualified stair window owns one Alt action before an
        # attack-target pause can interrupt it.
        return bool(
            self._stair_state.get("stall_frames", 0)
            >= max(1, self.stair_jump_stall_frames - 1)
        )

    def set_yolo_detection_active(self, active: bool) -> None:
        """Switch the jump-rope logic between YOLO screen and minimap.

        Fixed Attack mode runs without the YOLO subprocess, so there is no
        fresh screen gap to consult: the minimap logic must own the jump.
        """

        active = bool(active)
        if active != self._yolo_detection_active:
            LOG.info("rope jump logic: %s",
                     "YOLO screen" if active else "minimap only")
            self._yolo_detection_active = active

    def set_other_player_check(self, enabled: bool) -> None:
        """Enable/disable the automatic channel switch on other players."""

        self._other_player_check_enabled = bool(enabled)
        LOG.info("other-player channel switch: %s",
                 "on" if enabled else "off")

    def set_other_player_request_message(self, message: object) -> None:
        """Set the optional one-time message sent before a player switch."""

        self._other_player_request_message = str(message or "").strip()[:500]

    def set_other_player_channel_routing(
        self, *, room_code: object = "", wait_minutes: object = 5.0,
        current_channel: object = None, on_channel_landed: Any = None,
    ) -> None:
        """Apply persisted routing state without coupling this worker to Tk/config."""

        with self._other_player_settings_lock:
            self._other_player_room_code = str(room_code or "").strip()[:128]
            try:
                minutes = float(wait_minutes)
            except (TypeError, ValueError):
                minutes = 5.0
            self._other_player_wait_minutes = max(1.0, min(20.0, minutes))
            if current_channel is not None:
                self._current_channel = normalize_channel(current_channel)
            if on_channel_landed is not None:
                self._on_channel_landed = on_channel_landed

    def _other_player_routing_snapshot(self) -> tuple[str, float, int, Any]:
        with self._other_player_settings_lock:
            return (
                self._other_player_room_code,
                self._other_player_wait_minutes,
                self._current_channel,
                self._on_channel_landed,
            )

    def _note_channel_landed(self, channel: int) -> None:
        """Commit a fully sent channel route and notify the owning coordinator."""

        with self._other_player_settings_lock:
            self._current_channel = normalize_channel(channel)
            callback = self._on_channel_landed
        if callable(callback):
            try:
                callback(self._current_channel)
            except Exception:
                LOG.warning("player channel landing callback failed", exc_info=True)

    def _wait_for_player_departure(self, seconds: float) -> bool:
        """Watch fresh minimap frames; True means the player left before timeout."""

        deadline = time.monotonic() + max(0.0, float(seconds))
        while not self.stop_event.is_set():
            if self._other_players_on_latest_frame() == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            self.stop_event.wait(min(OTHER_PLAYER_PRESENCE_POLL_SECONDS, remaining))
        return False

    def _maybe_check_other_players(
        self, now: float, frame: Any, minimap_region: Any
    ) -> None:
        """Per-frame red-marker scan that starts the staged player workflow."""

        if not self._other_player_check_enabled:
            return
        if self._player_switch_active:
            return
        count = self._other_players_on_minimap(frame, minimap_region)
        if count > 0:
            self._trigger_other_player_switch(count)

    def _other_players_on_minimap(
        self, frame: Any, minimap_region: Any
    ) -> int:
        """Count red diamonds (other players) in the current minimap crop."""

        try:
            image = _image_from_frame(frame)
            minimap, _box = _crop(image, minimap_region)
            return len(detect_red_diamonds(minimap))
        except Exception:
            LOG.warning("other-player scan failed", exc_info=True)
            return 0

    def _trigger_other_player_switch(self, count: int) -> None:
        """Start the channel switch in a background thread (guarded once)."""

        if self._player_switch_active:
            LOG.info("player switch skipped: already switching")
            return
        self._player_switch_active = True
        LOG.warning("OTHER PLAYER detected on the minimap (%d); pausing before re-check", count)
        threading.Thread(
            target=self._run_other_player_switch, daemon=True
        ).start()

    def _run_other_player_switch(self) -> None:
        """Run the deliberate request/wait/route-plan player-switch workflow."""

        try:
            # The patrol must not fight the menu navigation keys.
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(False)
            # The first three minutes intentionally remain a quiet pause. The
            # request is one non-repeating message at a random instant in its
            # final two minutes, so no UI or routing decision leaks in here.
            request_after = random.uniform(
                OTHER_PLAYER_REQUEST_MIN_SECONDS, OTHER_PLAYER_REQUEST_MAX_SECONDS
            )
            if self.stop_event.wait(request_after):
                return
            message = self._other_player_request_message
            if message:
                sent = send_game_chat_message(self.key_sender, message)
                LOG.info("other-player request message %s", "sent" if sent else "not sent")
            if self.stop_event.wait(OTHER_PLAYER_INITIAL_PAUSE_SECONDS - request_after):
                return
            LOG.info("other-player request pause complete; monitoring for %.0fs", OTHER_PLAYER_MONITOR_SECONDS)
            if self._wait_for_player_departure(OTHER_PLAYER_MONITOR_SECONDS):
                LOG.warning("other player left during the monitor window; resuming patrol")
                return
            room_code, wait_minutes, _channel, _callback = self._other_player_routing_snapshot()
            LOG.warning(
                "other player remained for the monitor window; waiting %.1f minute(s) before channel switch",
                wait_minutes,
            )
            # Once this wait begins, disappearance does not revoke the switch
            # decision. This matches the operator's "keep change too" rule.
            if self.stop_event.wait(wait_minutes * 60.0):
                return
            attempts = 0
            while not self.stop_event.is_set():
                attempts += 1
                room_code, _wait_minutes, current_channel, _callback = self._other_player_routing_snapshot()
                route = plan_next_channel(current_channel, room_code)
                LOG.warning(
                    "player channel route %d: %d -> %d via %s%s",
                    attempts, route.current, route.target, route.moves,
                    " (房间码)" if room_code else " (随机)",
                )
                ok = channel_switch_procedure(
                    self.key_sender,
                    moves=route.moves,
                    on_press=lambda key, sent: LOG.info(
                        "player-switch press %s ok=%s", key, sent
                    ),
                )
                if not ok:
                    LOG.warning("player channel switch blocked; aborting")
                    break
                self._note_channel_landed(route.target)
                if self.stop_event.wait(self.other_player_switch_settle_seconds):
                    return
                count = self._other_players_on_latest_frame()
                if count == 0:
                    LOG.warning("player channel switch done (attempt %d); "
                                "new channel clean", attempts)
                    break
                LOG.warning("other players still present after switch "
                            "attempt %d (%d); switching again",
                            attempts, count)
        except Exception:
            LOG.exception("player channel switch failed")
        finally:
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(True)
            self._player_switch_active = False

    def _bottom_recorded_layer(self) -> Optional[str]:
        """Return the physical bottom recorded floor, independent of route.

        ``first_layer`` is the configured patrol-range start and can therefore
        be layer2/layer3.  Self-rescue must instead descend to the map's real
        bottom before handing control back to return-to-route.
        """

        floors = [
            name for name, layer in self.important_positions.items()
            if _has_layer_y_supporter(layer)
        ]
        return min(floors, key=_layer_number) if floors else None

    def _drop_to_first_layer(self) -> Optional[str]:
        """Drop through platforms until the physical bottom floor is reached.

        After a channel change the character can spawn on ANY layer (not
        necessarily the patrol-range start).  Before every Alt+Down chord the
        latest marker is matched against *all* recorded floor bands.  This is
        deliberately marker-first and does not trust a stale world-Y anchor.
        Capped so a stuck character cannot drop forever.
        """

        bottom_floor = self._bottom_recorded_layer()
        if bottom_floor is None:
            LOG.warning("self-rescue drop: no recorded floor is available")
            return None
        max_attempts = 30
        # A marker that already reads at-or-below the bottom floor's band is
        # treated as "bottom reached": dropping further is impossible (the
        # character is on/under the map's lowest floor) and repeating Alt+Down
        # there would only push the character deeper / into the void.  Same
        # at-or-below tolerance as the final-drop arrival check.  Without this
        # a character knocked into a pit under the bottom floor (marker just
        # outside every band) made the descent send Alt+Down chords over and
        # over (observed: endless "jump down" under the layer1 rope).
        bottom_layer = self.important_positions.get(bottom_floor, {})
        bottom_band = (
            _layer_y_band(
                bottom_layer,
                float(bottom_layer.get("y_tolerance", 0.020000)),
            )
            if isinstance(bottom_layer, dict) else None
        )
        for attempt in range(1, max_attempts + 1):
            if (self.reconnect_active_event is not None
                    and self.reconnect_active_event.is_set()):
                LOG.info(
                    "self-rescue drop: cancelled because auto reconnect owns input"
                )
                return None
            obs = self.last_observation
            # Do not press Alt+Down blindly when the marker disappears part
            # way through a rescue.  This is the same ownership boundary as
            # the early missing-marker return in _rescue_stuck_check(): the
            # disconnect/login detector decides whether reconnect is needed;
            # a map without its marker must receive no speculative movement.
            if obs is None or obs.player is None:
                LOG.warning(
                    "self-rescue drop: yellow marker unavailable; aborting "
                    "descent without sending further Alt+Down"
                )
                return None
            # This is a physical descent, so a world-Y estimate must never
            # finish it by itself. Right after reconnect the tracker can
            # still describe the old floor while the marker is visibly far
            # above the bottom layer. Only a marker-band match (or a marker
            # below the real bottom) proves that dropping is complete.
            detected_floor = None
            if obs is not None and obs.player is not None:
                detected_floor = detect_layer_by_y(
                    obs.player.y, {bottom_floor: bottom_layer}
                )
            if detected_floor == bottom_floor:
                self._reanchor_tracker_to_layer(bottom_floor, obs)
                LOG.info(
                    "self-rescue drop: reached physical bottom %s "
                    "(attempt %d); patrol start is %s",
                    bottom_floor, attempt, self.first_layer,
                )
                return bottom_floor
            if (obs is not None and obs.player is not None
                    and bottom_band is not None
                    and obs.player.y >= bottom_band[1] - 1e-9):
                self._reanchor_tracker_to_layer(bottom_floor, obs)
                LOG.warning(
                    "self-rescue drop: marker y=%.6f already at/below the %s "
                    "band (bottom %.6f); no drop needed",
                    obs.player.y, bottom_floor, bottom_band[1],
                )
                return bottom_floor
            LOG.info(
                "self-rescue drop: current=%s target=%s attempt %d/%d "
                "(Alt+Down)",
                detected_floor or "unknown", bottom_floor,
                attempt, max_attempts,
            )
            try:
                self._send_drop_through_platform()
            except Exception:
                LOG.warning("drop suppressed during self-rescue descent",
                            exc_info=True)
            time.sleep(self.drop_retry_seconds)
        LOG.warning(
            "self-rescue drop: gave up after %d attempts without confirming %s",
            max_attempts, bottom_floor,
        )
        return None

    def _restart_patrol_from_first_layer(
        self, landed_floor: Optional[str]
    ) -> None:
        """Queue patrol/return state from the floor rescue actually reached."""

        if landed_floor is None or not self._route_layers:
            return
        # Apply on the movement thread after the controller is re-enabled.
        # If the physical bottom is below the configured patrol range this
        # naturally enters climb-to-route instead of pretending it is layer2.
        self.prepare_patrol_start(landed_floor)
        LOG.info(
            "self-rescue complete on %s; queued fresh patrol/return detection",
            landed_floor,
        )

    def _other_players_on_latest_frame(self) -> int:
        """Red-diamond count on the most recent loop frame (post-switch)."""

        frame = getattr(self, "_last_frame", None)
        region = getattr(self, "_last_minimap_region", None)
        if frame is None or region is None:
            return 0
        return self._other_players_on_minimap(frame, region)

    def _drug_if_hp_low(self) -> None:
        """Eat an HP drug only when the current HP is below the threshold.

        Reads the latest HP ratio from the StatusWorker's shared state file
        (work/status_state.json) and the bound HP potion key from
        drug_settings.json; taps the key ``other_player_drug_taps`` times
        with gaps so the character survives the channel change.
        """

        hp = self._current_hp_ratio()
        if hp is None:
            LOG.info("hp unknown; skipping drug before channel switch")
            return
        if hp >= self.other_player_hp_threshold:
            LOG.info("hp %.0f%% >= %.0f%%; no drug needed",
                     hp * 100.0, self.other_player_hp_threshold * 100.0)
            return
        key = self._hp_drug_key()
        if key is None:
            LOG.warning("no sendable hp drug key; skipping drug")
            return
        LOG.warning("hp %.0f%% below %.0f%%; eating drug (%d x %s)",
                    hp * 100.0, self.other_player_hp_threshold * 100.0,
                    self.other_player_drug_taps, key)
        for _ in range(self.other_player_drug_taps):
            self.key_sender.press(key, duration=0.06)
            time.sleep(self.other_player_drug_gap_seconds)

    def _current_hp_ratio(self) -> Optional[float]:
        """Latest HP ratio (0..1) from the StatusWorker state file."""

        try:
            data = json.loads(
                Path(self.status_state_path).read_text(encoding="utf-8")
            )
            ratio = float(data.get("hp_ratio", -1.0))
            if 0.0 <= ratio <= 1.0:
                return ratio
        except (OSError, ValueError):
            pass
        return None

    def _hp_drug_key(self) -> Optional[str]:
        """The bound HP potion key from drug_settings.json, or None."""

        try:
            source = self.drug_settings_path
            data = json.loads(
                source.read_text(encoding="utf-8")
                if hasattr(source, "read_text")
                else Path(source).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        key = str(data.get("hp_key", "")).strip().casefold()
        if not key:
            return None
        scan_map = getattr(self.key_sender, "_SCAN", None)
        if scan_map is not None and key not in scan_map:
            return None
        return key

    def _yolo_rope_action(self) -> Optional[MovementDecision]:
        """Decide the rope action from YOLO SCREEN positions only.

        Compares the character's screen X with the rope's screen X (both
        from the YOLO subprocess) - never the minimap position:

        - while a climb is in progress (Up held / non-idle phase) and the
          character overlays the rope (|gap| <= on_rope_px) : return a
          no-op decision so patrol's climb state keeps holding Up
        - otherwise, when the character is right under the rope
          (|gap| <= under_rope_px) : jump straight up (Alt+Up) - a
          left/right chord from directly under the rope shoves the
          character past it
        - otherwise, when |gap| <= rope_jump_px : jump onto the rope, in
          the real screen direction (left or right).  This includes the
          initial grab from the ground: an idle character aligned with the
          rope (small gap) must still JUMP to attach, never wait.
        - otherwise (too far / stale / no rope) : return None, meaning the
          patrol (minimap) walk plan takes over
        """

        if not self._yolo_detection_active:
            # Fixed Attack mode: no YOLO subprocess, so the screen gap does
            # not exist - the minimap logic decides everything.
            return None
        if self._rope_state is None or not self._rope_state.is_fresh():
            return None
        gap = self._rope_state.screen_gap()
        if gap is None:
            return None
        climbing = bool(
            self._climb_state.up_held
            or self._climb_state.phase != "idle"
        )
        attached = self._climb_state.phase == "climbing-up"
        if attached and abs(gap) <= self.on_rope_px:
            # Genuinely attached and overlaying the rope: no jump - patrol
            # keeps holding Up and climbs.
            LOG.info("YOLO rope: on rope (gap=%+.0fpx); patrol climbs", gap)
            return MovementDecision(
                None, "YOLO: on rope; patrol holds Up to climb"
            )
        if abs(gap) > self.rope_jump_px:
            # Too far to jump: hand the approach back to patrol (minimap
            # walk).  YOLO never issues walking nudges - that is patrol's job.
            LOG.debug("YOLO rope: gap=%+.0fpx too large; patrol walks", gap)
            return None
        if abs(gap) <= self.under_rope_px or self._rope_state.x_overlap():
            # Directly under the rope (tight center gap OR the character box
            # horizontally overlaps the thin rope box): straight-up jump.  A
            # left/right chord here would push the character past the rope
            # and miss the grab.  The box-overlap test catches the under-rope
            # stance even when the box centers differ by 10-40px.
            decision = MovementDecision(
                "jump_climb_up",
                f"YOLO rope gap {gap:+.0f}px; right under rope, jump straight up",
                self.minimum_final_hold_seconds,
            )
            LOG.info("YOLO rope jump: gap=%+.0fpx dir=up (climbing=%s)",
                     gap, climbing)
            return decision
        direction = "right" if gap > 0 else "left"
        decision = MovementDecision(
            f"jump_climb_{direction}",
            f"YOLO rope gap {gap:+.0f}px; jump {direction} onto rope",
            self.minimum_final_hold_seconds,
        )
        LOG.info("YOLO rope jump: gap=%+.0fpx dir=%s (climbing=%s)",
                 gap, direction, climbing)
        return decision

    def _current_route_layer_with_grace(
        self, observation: MinimapObservation
    ) -> Optional[str]:
        """The floor the route is on, when the marker reads just BELOW its band.

        The marker centre carries a one-pixel quantisation (``LAYER_CURRENT_Y_GRACE``), larger than the
        band's own downward side, so a character standing at the low end of the floor it is already
        patrolling reads as "no layer" and is then treated as being on a rope / off the route.  This
        answers only that question: the strict ``_layer_y_band`` (and therefore the CLIMB arrival test)
        is unchanged.
        """

        if observation.player is None or not self._route_layers:
            return None
        index = self._route_layer_index
        if index is None or not 0 <= index < len(self._route_layers):
            return None
        name = self._route_layers[index]
        layer = self.important_positions.get(name)
        if not isinstance(layer, dict):
            return None
        band = _layer_y_band(layer, float(layer.get("y_tolerance", 0.020000)))
        if band is None:
            return None
        if band[1] <= observation.player.y <= band[1] + LAYER_CURRENT_Y_GRACE:
            LOG.info(
                "LAYER grace: marker Y=%.4f is %.4f below %s's band (<= %.4f); keeping %s",
                observation.player.y,
                observation.player.y - band[1],
                name,
                LAYER_CURRENT_Y_GRACE,
                name,
            )
            return name
        return None

    def _nearest_floor_by_marker_y(
        self,
        marker_y: float,
        layers: Optional[dict[str, Any]] = None,
        *,
        max_distance: Optional[float] = None,
    ) -> Optional[str]:
        """The recorded floor whose own positions are closest to the marker Y.

        The operator's rule for a reading that matches no band at all: "if the character can't find a
        layer he should anchor to the nearest layer".  It is the right answer for a stair/bench floor
        whose recorded points do not cover its whole vertical extent (his layer1 is one: its points were
        saved at 0.676829 while the character stands at 0.713415 further down the same platform), for a
        character that walked a step down, and for a marker just above the top floor's band (on a rope).
        Only floors the patrol range uses are considered - a floor outside it is the out-of-range return
        logic's business.

        The distance is ``_layer_y_distance`` (to the nearest RECORDED position), the same measure the
        candidate ranking uses, so the two never disagree.
        """

        source = layers if layers is not None else {
            name: self.important_positions.get(name) for name in self._route_layers
        }
        best: Optional[tuple[float, str]] = None
        for name, layer in source.items():
            if not isinstance(layer, dict):
                continue
            distance = _layer_y_distance(layer, marker_y)
            if distance is None:
                continue
            if best is None or distance < best[0]:
                best = (distance, name)
        if best is None:
            return None
        if max_distance is not None and best[0] > max_distance:
            LOG.info(
                "LAYER nearest-floor fallback refused: marker y=%.6f is %.4f away from %s "
                "(maximum %.4f); keeping the floor unresolved",
                marker_y, best[0], best[1], max_distance,
            )
            return None
        LOG.info(
            "LAYER nearest-floor fallback: marker y=%.6f matches no band; the nearest recorded floor is "
            "%s (%.4f normalised away)",
            marker_y, best[1], best[0],
        )
        return best[1]

    def _bottom_floor_for_marker_y(self, marker_y: float) -> Optional[str]:
        """The recorded bottom floor when the marker reads at or below its band.

        Nothing is recorded lower than the bottom floor, so a marker there IS the bottom floor - the same
        rule the landing reconciliation and ``_detect_floor_all`` use.  It matters for the operator's
        scrolling minimap: layer1's remembered stance is 0.676829, but the same floor renders at 0.713415
        while the character walks along it, i.e. below layer1's band and outside every other band, so
        recognition answered "now on none" while the character was plainly patrolling layer1.
        """

        bottom_floor = self._bottom_recorded_layer()
        if bottom_floor is None:
            return None
        layer = self.important_positions.get(bottom_floor)
        if not isinstance(layer, dict):
            return None
        band = _layer_y_band(layer, float(layer.get("y_tolerance", 0.020000)))
        if band is None or marker_y < band[1] - 1e-9:
            return None
        return bottom_floor

    def _detected_layer(self, observation: MinimapObservation) -> Optional[str]:
        layers = {name: self.important_positions[name] for name in self._route_layers}
        marker_candidates = (
            _layer_y_candidates(observation.player.y, layers)
            if observation.player is not None else []
        )
        # A unique marker band is direct evidence of the visible floor. It
        # must beat scroll tracking: OpenCV can briefly phase-lock to a
        # repeated platform and report the previous floor after a good climb.
        if len(marker_candidates) == 1:
            return marker_candidates[0]
        has_world_calibration = any(
            isinstance(layer, dict) and "layer_world_y" in layer
            for layer in layers.values()
        )
        if (has_world_calibration
                and observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            detected = detect_layer_by_world_y(observation.world_y_diamonds, layers)
            if detected is not None:
                return detected
            # During migration, layers not yet re-recorded have only raw Y.
            # Restrict fallback to those legacy layers so a centered marker
            # cannot override a valid world-Y match.
            legacy_layers = {
                name: layer for name, layer in layers.items()
                if isinstance(layer, dict) and "layer_world_y" not in layer
            }
            if observation.player is not None and legacy_layers:
                legacy = detect_layer_by_y(observation.player.y, legacy_layers)
                if legacy is not None:
                    return legacy
            if observation.player is not None:
                bottom = self._bottom_floor_for_marker_y(observation.player.y)
                if bottom is not None and bottom in layers:
                    LOG.info(
                        "LAYER bottom-floor rule: marker y=%.6f is at/below %s's band and matches no "
                        "other band; reporting %s",
                        observation.player.y, bottom, bottom,
                    )
                    return bottom
                # This is only a nearby marker-quantisation recovery.  An
                # unbounded nearest choice can report the old route floor
                # while the character is falling through one or more other
                # recorded floors, preventing the all-layer resolver from
                # handing that fall to return recovery.
                nearest = self._nearest_floor_by_marker_y(
                    observation.player.y,
                    layers,
                    max_distance=LAYER_NEAREST_FLOOR_MAX_DISTANCE,
                )
                if nearest is not None:
                    return nearest
            return self._current_route_layer_with_grace(observation)
        if observation.player is None:
            return None
        detected = detect_layer_by_y(observation.player.y, layers)
        if detected is not None:
            return detected
        bottom = self._bottom_floor_for_marker_y(observation.player.y)
        if bottom is not None and bottom in layers:
            LOG.info(
                "LAYER bottom-floor rule: marker y=%.6f is at/below %s's band and matches no other "
                "band; reporting %s",
                observation.player.y, bottom, bottom,
            )
            return bottom
        nearest = self._nearest_floor_by_marker_y(
            observation.player.y,
            layers,
            max_distance=LAYER_NEAREST_FLOOR_MAX_DISTANCE,
        )
        if nearest is not None:
            return nearest
        return self._current_route_layer_with_grace(observation)

    def _select_route_layer(self, observation: MinimapObservation | Point) -> None:
        if isinstance(observation, Point):
            observation = MinimapObservation(
                observation, None, 1.0, (0, 0, 1, 1)
            )
        if self._route_layer_index is not None or not self._route_layers:
            return
        detected_name = self._detected_layer(observation)
        if detected_name is None:
            if len(self._route_layers) == 1:
                self._route_layer_index = 0
                LOG.warning(
                    "single-layer fallback: selected %s marker_y=%s world_y=%s",
                    self._route_layers[0],
                    f"{observation.player.y:.6f}" if observation.player else "unknown",
                    (f"{observation.world_y_diamonds:.3f}"
                     if observation.world_y_diamonds is not None else "unknown"),
                )
                return
            LOG.warning(
                "layer unknown: marker_y=%s world_y=%s structure=%.3f",
                f"{observation.player.y:.6f}" if observation.player else "unknown",
                (f"{observation.world_y_diamonds:.3f}"
                 if observation.world_y_diamonds is not None else "unknown"),
                observation.structure_confidence,
            )
            return
        self._route_layer_index = self._route_layers.index(detected_name)
        LOG.info("route starting on %s: left-most -> right-most -> rope",
                 self._route_layers[self._route_layer_index])

    def _layer_band_contains(self, layer_name: str, y: float) -> bool:
        """True when the marker Y is inside the layer's recorded-point band."""
        layer = self.important_positions.get(layer_name)
        if not _has_layer_y_supporter(layer):
            return False
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None:
            return False
        return bool(band[0] - 1e-9 <= y <= band[1] + 1e-9)

    def _climb_arrival_band_contains(self, layer_name: str, y: float) -> bool:
        """True when a rope-top marker is within the next floor's arrival band.

        The normal layer matcher is deliberately exact: broadening it would
        make adjacent floors alias while walking.  A climb arrival is a
        different question.  The yellow diamond lands in one-pixel rows, so
        the state machine already gives the *next* floor one marker-row of
        room in :meth:`_next_layer_arrival_band`.  Route resync must use the
        same rule; otherwise ``climb()`` can correctly keep Up at the top but
        the resync confirmation never starts and the character remains stuck
        in ``holding-up-awaiting-progress``.
        """

        layer = self.important_positions.get(layer_name)
        if not _has_layer_y_supporter(layer):
            return False
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None:
            return False
        margin = _layer_marker_row(layer) or 0.0
        return bool(
            band[0] - margin - 1e-9 <= y <= band[1] + margin + 1e-9
        )

    def _on_route_floor(self, y: float) -> bool:
        """True when the marker Y sits inside the CURRENT route layer's band."""
        if (self._route_layer_index is None
                or not 0 <= self._route_layer_index < len(self._route_layers)):
            return False
        return self._layer_band_contains(
            self._route_layers[self._route_layer_index], y
        )

    def _nearest_world_layer_all(self, world_y: float) -> Optional[str]:
        """World-band floor over EVERY recorded layer (patrol range or not).

        The world-Y tracker re-anchors per floor, so after any move (climb,
        fall/DROP even to a floor outside the patrol range, e.g. layer1) the
        reading points at the NEW floor's anchor even when the minimap marker
        Y aliases inside the current band.  Runs every frame: this is what
        \"detect the layer each frame\" means for tracking the actual floor.
        """
        layers = {
            name: layer for name, layer in self.important_positions.items()
            if isinstance(layer, dict) and "layer_world_y" in layer
        }
        # Do not snap an arbitrary reading to whichever anchor is least far
        # away. The reading must lie in that layer's calibrated world band.
        return detect_layer_by_world_y(world_y, layers)

    def _resync_route_layer(self, observation: MinimapObservation) -> Optional[str]:
        """Switch patrol state when the marker is detected on another layer.

        Falling and failed climbs can invalidate the expected route layer.  We
        check every fresh minimap frame, but only switch after Y falls inside a
        calibrated layer tolerance; intermediate airborne positions are ignored.
        """

        if observation.player is None or not self._route_layers:
            self._clear_layer_resync_candidate()
            return None
        if self._descending_to_first:
            # The planned descent to the patrol route's FIRST floor owns the machine until the character
            # arrives there (``_final_drop_arrived`` -> ``_reset_route_loop``, or the
            # ``DROP_TO_FIRST_MAX_SECONDS`` bound).  Alt+Down drops one platform per chord, so the marker
            # sweeps through the floors in between and the character even STANDS on the next floor up
            # between chords - the normal resync confirmed exactly that as "LAYER CHANGED: layer3 ->
            # layer2; restarting layer2 patrol", which ended the descent on layer2 and never returned the
            # loop to its first floor.
            #
            # It also blocks the knock-down and return-to-route recoveries (the operator: "the back to
            # base patrol layer should block the hit down by monster function, don't trigger back to
            # patrol route").  Everything that could take the machine away mid-descent is suppressed for
            # that reason: this resync, ``_track_fall``, ``_verify_out_of_range_floor`` and the
            # self-rescue.  The descent itself is bounded, so suppressing them cannot deadlock.
            current = (
                self._route_layers[self._route_layer_index]
                if (self._route_layer_index is not None
                    and 0 <= self._route_layer_index < len(self._route_layers))
                else None
            )
            seen = _layer_y_candidates(
                observation.player.y, self.important_positions
            )
            seen_route = [name for name in seen if name in self._route_layers]
            if seen_route and current is not None and seen_route[0] != current:
                if self._drop_descent_saw != seen_route[0]:
                    self._drop_descent_saw = seen_route[0]
                    LOG.info(
                        "DROP TO FIRST: the marker is on %s during the planned descent; keeping the route "
                        "on %s until %s is reached (the descent owns the floors in between, and no "
                        "knock-down/return logic may interrupt it)",
                        seen_route[0], current, self.first_layer,
                    )
            self._clear_layer_resync_candidate()
            return current
        if self._return_mode is not None:
            # Return-to-route owns the route state until it explicitly hands
            # patrol back to an in-range floor in ``_finish_return``.  The
            # normal resync used to see the stale pre-fall route index here
            # (for example layer3) and turn the successful layer1 -> layer2
            # return into a generic "layer3 -> layer2" backward transition.
            # That reset raced the dedicated return cleanup and could leave
            # layer2 repeating instead of advancing to layer3.
            self._clear_layer_resync_candidate()
            return self._detect_floor_all(observation)
        climb_input_active = (
            self._climb_state.up_held
            or self._climb_state.phase == "climbing-up"
        )
        # Evidence for the transition logs, filled by the non-climb path below; empty while climbing so
        # the tail can always name what the marker matched.
        marker_candidates_all: list[str] = []
        marker_candidates_route: list[str] = []
        expected_next_index = (
            self._route_layer_index + 1
            if self._route_layer_index is not None else -1
        )
        # Arrival is first confirmed by consecutive layer-detection frames.
        # After that signal becomes stable, keep Up owned for a short bounded
        # compensation window so the character clears the rope lip before the
        # route advances.  This is timestamped (not a blocking sleep), so the
        # worker continues consuming frames and coordinating other actions.
        compensating = bool(
            climb_input_active
            and self._climb_state.target_layer_since is not None
            and 0 <= expected_next_index < len(self._route_layers)
        )
        if compensating:
            elapsed = time.monotonic() - self._climb_state.target_layer_since
            expected_name = self._route_layers[expected_next_index]
            if elapsed < self.climb_layer_confirm_seconds:
                LOG.info(
                    "CLIMB top compensation: %s %.2f/%.2fs; keeping Up held",
                    expected_name,
                    elapsed,
                    self.climb_layer_confirm_seconds,
                )
                return self._route_layers[self._route_layer_index]
            # The target was already frame-confirmed before compensation
            # began.  Do not let one rope-top animation frame undo it.
            detected_name = expected_name
        elif climb_input_active:
            # A climb can only arrive at the immediate next route layer.  The
            # old nearest-anchor rule switched at the midpoint between floors,
            # released Up while the character was still on the rope, and then
            # horizontal patrol pulled it off.  Accept the next floor only
            # from an unambiguous marker band or when world Y is tightly near
            # that floor's calibrated anchor.
            current_name = (
                self._route_layers[self._route_layer_index]
                if (self._route_layer_index is not None
                    and 0 <= self._route_layer_index < len(self._route_layers))
                else None
            )
            expected_name = (
                self._route_layers[expected_next_index]
                if 0 <= expected_next_index < len(self._route_layers)
                else None
            )
            marker_expected = bool(
                expected_name is not None
                and observation.player is not None
                and self._climb_arrival_band_contains(
                    expected_name, observation.player.y
                )
            )
            marker_current = bool(
                current_name is not None
                and observation.player is not None
                and self._layer_band_contains(
                    current_name, observation.player.y
                )
            )
            marker_unambiguous = marker_expected and not marker_current
            marker_route_name = detect_layer_by_y(
                observation.player.y,
                {
                    name: self.important_positions[name]
                    for name in self._route_layers
                },
            )
            marker_lower_fall = bool(
                marker_route_name is not None
                and self._route_layer_index is not None
                and self._route_layers.index(marker_route_name)
                    < self._route_layer_index
                and not marker_current
            )

            world_expected = False
            if (expected_name is not None
                    and observation.world_y_diamonds is not None
                    and observation.structure_confidence >= 0.12):
                expected_layer = self.important_positions.get(expected_name, {})
                current_layer = self.important_positions.get(current_name, {})
                if (isinstance(expected_layer, dict)
                        and "layer_world_y" in expected_layer):
                    expected_world = float(expected_layer["layer_world_y"])
                    world_tolerance = self.climb_arrival_world_tolerance
                    if (isinstance(current_layer, dict)
                            and "layer_world_y" in current_layer):
                        anchor_gap = abs(
                            expected_world
                            - float(current_layer["layer_world_y"])
                        )
                        # Closely spaced anchors need a proportionally tighter
                        # gate; otherwise both floors fall inside the maximum.
                        world_tolerance = min(
                            world_tolerance,
                            max(0.01, anchor_gap * 0.25),
                        )
                    world_expected = (
                        abs(observation.world_y_diamonds - expected_world)
                        <= world_tolerance
                    )
                    # A successful rope climb can re-anchor the minimap world
                    # tracker at the platform edge rather than exactly at the
                    # layer's canonical centre.  The persisted world band is
                    # the recording-time envelope for that floor, and is
                    # reliable *only* when the reading has left the departing
                    # floor's own envelope.  Use it as an arrival fallback so
                    # a legitimate layer2 landing begins the normal 4-frame
                    # confirmation instead of waiting forever in the rope
                    # state.  Keeping the "not current" gate prevents a broad
                    # world band from accepting the jump before the character
                    # has actually left layer1.
                    expected_band = _layer_world_y_band(
                        expected_layer,
                        float(expected_layer.get("world_y_tolerance", 0.75)),
                    )
                    current_band = (
                        _layer_world_y_band(
                            current_layer,
                            float(current_layer.get("world_y_tolerance", 0.75)),
                        )
                        if isinstance(current_layer, dict)
                        else None
                    )
                    world_in_expected_band = bool(
                        expected_band is not None
                        and expected_band[0] - 1e-9
                        <= observation.world_y_diamonds
                        <= expected_band[1] + 1e-9
                    )
                    world_in_current_band = bool(
                        current_band is not None
                        and current_band[0] - 1e-9
                        <= observation.world_y_diamonds
                        <= current_band[1] + 1e-9
                    )
                    world_expected = world_expected or (
                        world_in_expected_band and not world_in_current_band
                    )
            detected_name = (
                expected_name
                if marker_unambiguous or world_expected
                else marker_route_name if marker_lower_fall
                else current_name if marker_current else None
            )
        else:
            detected_name = self._detected_layer(observation)
            marker_candidates_all = _layer_y_candidates(
                observation.player.y, self.important_positions
            )
            # Only the floors the PATROL RANGE uses may make the marker reading "ambiguous".  A recorded
            # floor the range does not use (a leftover/stale layer in the profile, or another map's
            # recording) used to count here, and one overlapping band was then enough to hand the floor
            # decision to the world-Y tracker - the operator's 13:16 log is exactly that: the character
            # was started on layer1, player_y 0.676829 sat inside layer1's band, and the worker answered
            # "LAYER CHANGED: layer1 -> layer2 at y=0.676829" from the world signal.  An out-of-route
            # match is still honoured below (it returns None so the fall/return recovery owns it).
            marker_candidates_route = [
                name for name in marker_candidates_all
                if name in self._route_layers
            ]
            marker_is_unambiguous = len(marker_candidates_route) == 1
            if marker_is_unambiguous:
                detected_name = marker_candidates_route[0]
            # World-nearest override: every frame, over EVERY recorded
            # floor.  After a fall the tracker re-anchors to the new floor
            # (even layer1, outside the patrol range), so the world read
            # points there even when the marker Y aliases inside the
            # current band.  The flicker guard below then only protects
            # the current floor while the world anchor still matches it.
            world_name = (
                self._nearest_world_layer_all(observation.world_y_diamonds)
                if (observation.world_y_diamonds is not None
                    and observation.structure_confidence >= 0.12)
                else None
            )
            if world_name is not None:
                # The world signal may not name a floor the marker draws the character BELOW: that is the
                # physical limit already used for fall landings (see ``_landing_floor_cap``).  The
                # operator's 14:09 log is why it is needed here too: the character was walking on layer1
                # with the marker at 0.713415 (below layer1's band, outside every band), and a poisoned
                # world reading of 1.204824 named layer2 - one more matching frame and the patrol would
                # have switched to a floor the character was visibly under.
                world_name = self._cap_landing_floor(
                    world_name, observation, source="the world-Y signal"
                )
            if (not marker_is_unambiguous
                    and world_name is not None
                    and world_name != detected_name):
                # The evidence is logged with the decision: "why did it change floor?" must be
                # answerable from the log alone (his 13:16 report could not be).
                LOG.info(
                    "LAYER world override: marker_y=%.6f matches %s (out of range), world=%s "
                    "world_y=%.6f confidence=%.3f; following the world signal",
                    observation.player.y,
                    ",".join(marker_candidates_all) or "no floor",
                    world_name,
                    observation.world_y_diamonds,
                    observation.structure_confidence,
                )
                detected_name = world_name
            elif (marker_is_unambiguous
                    and world_name is not None
                    and world_name != detected_name):
                LOG.info(
                    "LAYER signal disagreement: marker=%s world=%s "
                    "world_y=%.6f confidence=%.3f; marker wins",
                    marker_candidates_route[0], world_name,
                    observation.world_y_diamonds,
                    observation.structure_confidence,
                )
            elif detected_name is None and observation.player is not None:
                # The patrol range is not the map.  A fall may land on a
                # recorded floor outside that range (for example layer1
                # beneath a layer2 -> layer3 route).  Resolve across ALL
                # recorded floors before considering a nearest-Y fallback;
                # otherwise one missing world sample makes the route-only
                # fallback call that landing "layer3" merely because it is
                # the closest route point.
                detected_name = self._detect_floor_all(observation)
                if detected_name is None:
                    # A nearest-Y answer is useful for a nearby unsampled
                    # platform edge, but never for a multi-floor gap while
                    # falling.  The bounded fallback leaves that latter case
                    # unresolved for fall/return reconciliation instead of
                    # restarting a distant patrol floor.
                    detected_name = self._nearest_floor_by_marker_y(
                        observation.player.y,
                        {name: self.important_positions[name]
                         for name in self._route_layers},
                        max_distance=LAYER_NEAREST_FLOOR_MAX_DISTANCE,
                    )
            # Overlapping-band flicker guard: adjacent floors' recorded
            # Y bands can overlap (span +- tolerance), so a Y-only reading
            # can hit BOTH the current floor and a neighbour (observed:
            # "LAYER CHANGED: layer3 -> layer2 at y=0.348958" while the
            # character visibly stands on layer3).  When the CURRENT
            # layer's own band still contains the marker Y, keep patrolling
            # it - the switch would re-target the other floor's points and
            # the patrol never completes.  It applies ONLY to ambiguous
            # marker-Y-only detection: a confident scroll-compensated
            # world-Y read (structure confidence + calibrated world Y) is
            # still authoritative and switches floors.  Unambiguous marker
            # readings (clearly outside the current band) still switch.
            # A present world-Y sample is not evidence by itself.  It may be
            # between every recorded world band while the structure tracker
            # is settling after a scroll/fall.  Only a resolved world floor
            # may override the marker-band flicker guard.
            world_authoritative = world_name is not None
            current_name = (
                self._route_layers[self._route_layer_index]
                if (self._route_layer_index is not None
                    and 0 <= self._route_layer_index < len(self._route_layers))
                else None
            )
            if (current_name is not None
                    and detected_name is not None
                    and detected_name != current_name
                    and not world_authoritative
                    and observation.player is not None
                    and self._layer_band_contains(
                        current_name, observation.player.y
                    )
                    and (world_name is None or world_name == current_name)):
                LOG.info(
                    "LAYER flicker guard: keeping %s (Y %.6f still inside "
                    "its band)", current_name, observation.player.y
                )
                detected_name = current_name
        if (detected_name is not None
                and detected_name not in self._route_layers
                and self._fall_frames > 0):
            self._note_fall_floor_candidate(detected_name)
        if detected_name is None:
            self._clear_layer_resync_candidate()
            # Fall detection becomes pending after resync in the first stable
            # frame.  The previously observed lower floor may no longer be
            # visible now, so use the cached, confirmed candidate instead of
            # leaving the prior route layer armed.
            if (self._fall_pending
                    and self._return_mode is None
                    and self._fall_floor_candidate is not None):
                self._maybe_begin_return_if_out_of_range(
                    observation, confirmed_floor=self._fall_floor_candidate
                )
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
                # A blank frame is the character's own jump arc just above the
                # platform row (13:58 log: marker 0.617857 / world 2.71 while
                # layer2's row is 0.653571 / 3.546).  It is not evidence against
                # the arrival, and zeroing the confirmation here is what made an
                # arrival on a platform top impossible to complete.
                self._climb_state.blank_arrival_frames += 1
                if (self._climb_state.blank_arrival_frames
                        >= CLIMB_ARRIVAL_BLANK_FRAMES_TOLERATED):
                    self._climb_state.target_layer_frames = 0
                    self._climb_state.target_layer_since = None
            return None
        if detected_name not in self._route_layers:
            # Out-of-route floor (e.g. layer1 while the patrol range starts at
            # layer2): the world override can point at a floor OUTSIDE the
            # route after a fall/drop, and indexing it would crash
            # (ValueError: 'layer1' is not in list).  Never index an
            # out-of-route floor - the out-of-range return logic (fall
            # recovery / return-to-route) picks it up instead and climbs back
            # to the route start (user case: character starts on layer1 and
            # must return to layer2 before patrolling).
            self._clear_layer_resync_candidate()
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
                # A recognised floor is real evidence, so a blank-frame run ends
                # here even though this floor cannot be indexed.
                self._climb_state.blank_arrival_frames = 0
                if self._return_mode != "climb-to-route":
                    self._climb_state.target_layer_frames = 0
                    self._climb_state.target_layer_since = None
            # A settled fall has now supplied a real floor outside the route.
            # Do not keep sending the old route floor's horizontal patrol
            # while the separate fall reconciler gathers its final samples:
            # that is how a confirmed layer1 marker continued walking the
            # layer3 left/right route.  ``_fall_pending`` becomes true only
            # after the marker stopped descending, so an airborne layer2
            # reading cannot start a rope return in mid-air.
            if self._fall_pending and self._return_mode is None:
                self._maybe_begin_return_if_out_of_range(
                    observation, confirmed_floor=detected_name
                )
            return None
        self._climb_state.blank_arrival_frames = 0
        detected_index = self._route_layers.index(detected_name)
        if self._route_layer_index is None:
            self._clear_layer_resync_candidate()
            self._route_layer_index = detected_index
            self._route_phase = "left"
            self._route_patrol_cycle = 1
            LOG.info("route starting on %s: left-most -> right-most -> rope",
                     detected_name)
            return detected_name
        if detected_index == self._route_layer_index:
            self._clear_layer_resync_candidate()
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
                self._climb_state.target_layer_frames = 0
                self._climb_state.target_layer_since = None
            return detected_name

        expected_next_index = self._route_layer_index + 1
        if climb_input_active and detected_index == expected_next_index:
            self._climb_state.target_layer_frames += 1
            if self._climb_state.target_layer_frames < self.climb_layer_confirm_frames:
                LOG.info(
                    "CLIMB arrival confirmation: %s %d/%d; keeping Up held",
                    detected_name,
                    self._climb_state.target_layer_frames,
                    self.climb_layer_confirm_frames,
                )
                return self._route_layers[self._route_layer_index]
            if (self.climb_layer_confirm_seconds > 0
                    and self._climb_state.target_layer_since is None):
                self._climb_state.target_layer_since = time.monotonic()
                LOG.info(
                    "CLIMB layer %s confirmed; compensating Up for %.2fs",
                    detected_name,
                    self.climb_layer_confirm_seconds,
                )
                return self._route_layers[self._route_layer_index]
        elif climb_input_active:
            self._climb_state.target_layer_frames = 0
            self._climb_state.target_layer_since = None

        if not climb_input_active:
            # Never restart patrol on another layer after one noisy marker or
            # map-structure frame.  This was the cause of an observed
            # layer3 -> layer2 -> layer3 flip in one second: each restart
            # reset the route phase to left and made the character abruptly
            # walk Right, then Left again far from either endpoint.
            #
            # While a possible fall is already being measured, fall recovery
            # owns the landing transition and will resolve it from its stable
            # samples.  Normal resync must not race it.
            current_name = self._route_layers[self._route_layer_index]
            if self._fall_frames > 0 or self._fall_pending:
                self._clear_layer_resync_candidate()
                return current_name
            if self._layer_resync_candidate != detected_name:
                self._layer_resync_candidate = detected_name
                self._layer_resync_candidate_frames = 1
            else:
                self._layer_resync_candidate_frames += 1
            if self._layer_resync_candidate_frames < self._normal_layer_resync_frames:
                LOG.info(
                    "LAYER transition candidate: %s -> %s %d/%d; keeping "
                    "current patrol (marker_y=%.6f matches %s, world_y=%s "
                    "confidence=%.3f)",
                    current_name,
                    detected_name,
                    self._layer_resync_candidate_frames,
                    self._normal_layer_resync_frames,
                    (observation.player.y if observation.player is not None
                     else float("nan")),
                    ",".join(marker_candidates_all) or "no floor",
                    (f"{observation.world_y_diamonds:.6f}"
                     if observation.world_y_diamonds is not None else "n/a"),
                    observation.structure_confidence,
                )
                return current_name
            self._clear_layer_resync_candidate()

        previous_name = (
            self._route_layers[self._route_layer_index]
            if 0 <= self._route_layer_index < len(self._route_layers)
            else "route-complete"
        )
        was_climbing = climb_input_active
        if was_climbing:
            self._climb_arrival_at = time.monotonic()
        self._release_climb_up()
        self._route_layer_index = detected_index
        # The marker is confirmed on an in-range route layer: any below-range
        # rescue streak is over.
        self._rescue_cycles = 0
        self._climb_lateral_streak = 0
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._climb_state = ClimbState()
        self._aligned_frames = 0
        self._rope_approach_direction = None
        # First-vs-retry rope approach per layer: the FIRST time the
        # character moves to the rope it walks continuously like
        # move-to-left-most/right-most; only RETRY approaches (after a
        # failed jump) use the small creep steps.
        self._rope_attempted = False
        self._last_drop_attempt = float("-inf")
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        # Arrival is definitive: bypass the inter-attempt busy hysteresis so
        # both fixed and YOLO attacks can resume on this very frame.
        self._patrol_busy_until = 0.0
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.near_rope_event is not None:
            self.near_rope_event.clear()
        if was_climbing:
            # Confirmed rope arrival is stronger than an aliased OpenCV
            # structure result. Establish the new floor's world origin now.
            self._reanchor_tracker_to_current_layer(observation)
        returned_to_first = (
            self.first_layer is not None
            and detected_name == self.first_layer
            and previous_name != self.first_layer
        )
        if returned_to_first and not was_climbing:
            self._reanchor_tracker_to_current_layer(observation)
            anchor_world_y = self._current_layer_world_y()
            if anchor_world_y is not None:
                LOG.info("MAP LOOP reset world Y at %s=%.6f",
                         self.first_layer, anchor_world_y)
        if was_climbing:
            LOG.info("CLIMB complete: detected %s at y=%.6f",
                     detected_name, observation.player.y)
        else:
            LOG.warning(
                "LAYER CHANGED: %s -> %s at y=%.6f; restarting %s patrol "
                "(marker_y matches %s, world_y=%s confidence=%.3f)",
                previous_name, detected_name, observation.player.y, detected_name,
                ",".join(marker_candidates_all) or "no floor",
                (f"{observation.world_y_diamonds:.6f}"
                 if observation.world_y_diamonds is not None else "n/a"),
                observation.structure_confidence,
            )
        return detected_name

    def _floor_number(self, name: str) -> int:
        return _layer_number(name)

    def _in_patrol_range(self, floor: str) -> bool:
        number = _layer_number(floor)
        return self._patrol_range_min <= number <= self._patrol_range_max

    def _current_route_floor(self) -> Optional[str]:
        if (self._route_layer_index is None
                or not 0 <= self._route_layer_index < len(self._route_layers)):
            return None
        return self._route_layers[self._route_layer_index]

    def _clear_layer_resync_candidate(self) -> None:
        """Forget an unconfirmed cruising-layer transition."""

        self._layer_resync_candidate = None
        self._layer_resync_candidate_frames = 0

    def _stabilize_rope_target(
        self,
        target_x: Optional[float],
        is_rope: bool,
        label: str,
    ) -> Optional[float]:
        """Hold one rope phase's target X against per-frame projection jitter.

        The stored rope X is diamond-relative, so re-projecting it every frame
        through a slightly different measured diamond slides the target by up
        to ~0.02 - more than the rope's jump band.  The character then walks
        toward a moving target and the climb jump side flips with it.  The
        first sample of a rope phase is locked and then followed with a slow
        exponential average; a sample that really moved
        (``ROPE_TARGET_JITTER_BAND``) re-locks immediately, and a different
        phase label starts a fresh lock.
        """

        if not is_rope or target_x is None:
            self._held_rope_target = None
            return target_x
        sample = float(target_x)
        held = self._held_rope_target
        if held is None or held[0] != label:
            self._held_rope_target = (label, sample)
            LOG.info("ROPE TARGET: %s locked at x=%.6f", label, sample)
            return sample
        value = held[1]
        if abs(sample - value) > ROPE_TARGET_JITTER_BAND:
            LOG.info(
                "ROPE TARGET: %s re-locked from x=%.6f to x=%.6f (moved beyond "
                "the %.3f jitter band)",
                label, value, sample, ROPE_TARGET_JITTER_BAND,
            )
            self._held_rope_target = (label, sample)
            return sample
        smoothed = value + ROPE_TARGET_SMOOTHING_ALPHA * (sample - value)
        self._held_rope_target = (label, smoothed)
        return smoothed

    def _climb_preferred_direction(
        self,
        decision_key: str,
        observation: MinimapObservation,
        route_target_x: Optional[float],
    ) -> Optional[str]:
        """Lateral jump side for the next climb attempt, from the rope geometry.

        Returns ``None`` when the character is aligned within the dead band, so
        the caller keeps the side the approach actually came from.  A jump is
        never issued toward the side the character already overshot.
        """

        if decision_key == "jump_climb_up":
            return "up"
        player = observation.player
        if player is None or route_target_x is None:
            return None
        live_gap = route_target_x - player.x
        if live_gap > ROPE_JUMP_DIRECTION_DEAD_BAND:
            return "right"
        if live_gap < -ROPE_JUMP_DIRECTION_DEAD_BAND:
            return "left"
        return None

    def _log_climb_direction(
        self,
        direction: str,
        observation: MinimapObservation,
        route_target_x: Optional[float],
    ) -> None:
        """Log the climb direction decision once per attempt/direction change."""

        player = observation.player
        gap = (
            route_target_x - player.x
            if (player is not None and route_target_x is not None)
            else None
        )
        attempt = f"{self._climb_state.phase}:{direction}"
        if self._climb_state.phase == "idle" or self._climb_direction_log != attempt:
            self._climb_direction_log = attempt
            LOG.info(
                "CLIMB direction: rope_x=%s player_x=%s gap=%s -> Alt+%s "
                "(rope_phase_target_held=%s)",
                f"{route_target_x:.6f}" if route_target_x is not None else "----",
                f"{player.x:.6f}" if player is not None else "----",
                f"{gap:+.6f}" if gap is not None else "----",
                direction,
                self._held_rope_target is not None,
            )

    def prepare_patrol_start(
        self, floor: str, *, above_route: bool = False,
        reconnect_restart: bool = False,
    ) -> None:
        """Queue the independently detected startup floor for this worker.

        ``above_route`` covers a marker that is visibly above the highest
        recorded patrol layer but does not match any layer band.  It must
        start the shared drop-to-route state instead of being treated as the
        fallback layer passed solely for world-Y anchoring.
        """

        with self._patrol_start_lock:
            self._pending_patrol_start_floor = str(floor)
            self._pending_patrol_start_above_route = bool(above_route)
            self._pending_patrol_start_reconnect = bool(reconnect_restart)

    def set_stationary_attack_enabled(self, enabled: bool) -> None:
        """Select 站桩攻击, which is independent of recorded route data."""

        enabled = bool(enabled)
        if self.stationary_attack_enabled != enabled:
            self.stationary_attack_enabled = enabled
            self._stationary_attack_anchor = None
            self._stationary_ui_anchor_layer = None
            self._stationary_route_anchor_layer = None
            self._stationary_return_route_ready = False
            self._stationary_route_validation_error = ""
            self._stationary_facing_command = None
            self._stationary_bilateral_target = "right"
            self._stationary_bilateral_frames = 0
            self._stationary_x_settled = False
            self._stationary_near_correction_next_at = 0.0
            self._stationary_step_target_x = None
            self._stationary_facing_confirm_frames_remaining = 0
            self._stationary_pickup_phase = None
            self._stationary_pickup_failures = 0
            self._stationary_pickup_retry_after_return = False
            self._stationary_pickup_return_active = False
            self._stationary_return_dismount_direction = None
            self._reset_stationary_y_recovery()
            LOG.info(
                "stationary attack mode %s",
                "enabled; awaiting temporary Start Patrol position" if enabled else "disabled",
            )

    def stationary_route_anchor_marker(self) -> Optional[tuple[str, Point, str]]:
        """Temporary layer-axis marker and selected facing for this session."""

        anchor = self._stationary_attack_anchor
        layer = self._stationary_ui_anchor_layer
        if anchor is None or not layer:
            return None
        direction = str(self.stationary_facing_direction).casefold()
        if direction not in ("left", "right", "both"):
            direction = "right"
        return layer, anchor, direction

    def configure_stationary_return_route(self) -> bool:
        """Make the anchor's existing layer the one-layer stationary route.

        This deliberately does *not* add a layer or persist the temporary
        position.  It only changes the existing patrol range's end selection
        when the selected bottom-to-top range can genuinely lead to the
        anchor's layer.
        """

        self._stationary_ui_anchor_layer = None
        self._stationary_route_anchor_layer = None
        self._stationary_return_route_ready = False
        self._stationary_route_validation_error = ""
        if not self.stationary_attack_enabled:
            return False
        anchor = self._stationary_attack_anchor
        if anchor is None or self.patrol_controller is None:
            return False
        self._sync_patrol_controller()
        candidates = [
            name for name in self.important_positions
            if self._layer_band_contains(name, anchor.y)
        ]
        if not candidates:
            # An empty selected layer has no Y band yet, but it is still the
            # operator's chosen layer for a new stand-still attack.  The
            # configured route END is a stronger statement than the UI's
            # currently selected row: the latter is often layer1 merely
            # because the user last recorded its rope.  This lets a temporary
            # fixed point on an otherwise empty layer2 remain layer2, rather
            # than silently being assigned to the lower rope layer.
            _start, configured_end = self.patrol_controller.patrol_range()
            selected = self.patrol_controller.selected_layer()
            if configured_end in self.important_positions:
                anchor_layer = configured_end
                LOG.info(
                    "STATIONARY ATTACK: no calibrated Y band; using configured "
                    "destination %s for the temporary anchor", anchor_layer,
                )
            elif selected in self.important_positions:
                anchor_layer = selected
                LOG.info(
                    "STATIONARY ATTACK: no calibrated Y band; showing the "
                    "temporary anchor on selected %s", anchor_layer,
                )
            else:
                LOG.warning(
                    "STATIONARY RETURN: temporary anchor y=%.6f matches no "
                    "existing or selected layer", anchor.y,
                )
                return False
        else:
            anchor_layer = min(
                candidates,
                key=lambda name: abs(float(
                    self.important_positions[name].get("layer_y", anchor.y)
                ) - anchor.y),
            )
        self._stationary_ui_anchor_layer = anchor_layer
        LOG.info(
            "STATIONARY ATTACK: temporary anchor x=%.6f y=%.6f is marked "
            "on existing %s in the layer UI",
            anchor.x, anchor.y, anchor_layer,
        )
        previous_start, previous_end = self.patrol_controller.patrol_range()
        try:
            # The standing point defines this mode's route.  It does not
            # inherit a previous multi-layer patrol range, which made pickup
            # and recovery walk unrelated floors before returning to the
            # temporary anchor.
            self.patrol_controller.set_patrol_range(anchor_layer, anchor_layer)
        except (TypeError, ValueError):
            LOG.warning("STATIONARY RETURN: cannot select %s as route end", anchor_layer,
                        exc_info=True)
            return False
        route_errors = self.patrol_controller.validate_patrol_route()
        if route_errors:
            detail = "; ".join(route_errors)
            # With ordinary 站桩攻击 the temporary anchor is fully usable on
            # its own.  Do not change its historical no-recording behavior
            # merely because a nearby partial layer exists.  捡东西 is the
            # opt-in feature that needs edge records, so only it turns this
            # into a start-blocking configuration error.
            if self.stationary_pickup_enabled:
                self._stationary_route_validation_error = detail
                LOG.warning(
                    "STATIONARY ROUTE: %s is selected for the temporary anchor but "
                    "is not patrol-ready: %s",
                    anchor_layer, detail,
                )
                return False
            try:
                self.patrol_controller.set_patrol_range(
                    previous_start, previous_end
                )
            except (TypeError, ValueError):
                LOG.warning("STATIONARY ATTACK: could not restore patrol range",
                            exc_info=True)
                return False
            self._sync_patrol_controller()
            # Ordinary stand-still attack does not need edge recordings.  It
            # can nevertheless climb back from a lower recorded rope to this
            # temporary anchor.  Keep this small return-only route separate
            # from a patrol/pickup route so the loose recording rule remains
            # true for normal stand-still use.
            lower_ropes = [
                name for name, layer in self.important_positions.items()
                if (_layer_number(name) < _layer_number(anchor_layer)
                    and isinstance(layer, dict)
                    and isinstance(layer.get("rope_pos"), dict)
                    and "x" in layer["rope_pos"])
            ]
            if lower_ropes:
                self._stationary_route_anchor_layer = anchor_layer
                self._stationary_return_route_ready = True
                LOG.info(
                    "STATIONARY RETURN armed with temporary destination %s; "
                    "using lower rope layer(s) %s despite missing patrol edges",
                    anchor_layer, ", ".join(sorted(lower_ropes, key=_layer_number)),
                )
                return True
            LOG.warning(
                "STATIONARY ROUTE: %s is selected for the temporary anchor but "
                "is not return-ready: %s (no lower recorded rope)",
                anchor_layer, detail,
            )
            return False
        self._sync_patrol_controller()
        if (anchor_layer not in self._route_layers
                or not self._route_layers
                or self._route_layers[-1] != anchor_layer):
            LOG.warning(
                "STATIONARY RETURN: %s is not an action-bearing final patrol layer; "
                "no route recovery was armed", anchor_layer,
            )
            return False
        self._stationary_route_anchor_layer = anchor_layer
        self._stationary_return_route_ready = True
        self._route_layer_index = self._route_layers.index(anchor_layer)
        self._route_phase = "left"
        LOG.info(
            "STATIONARY RETURN armed: temporary anchor x=%.6f y=%.6f on %s; "
            "patrol range ends at that layer (%s)",
            anchor.x, anchor.y, anchor_layer, " -> ".join(self._route_layers),
        )
        return True

    def stationary_route_validation_error(self) -> str:
        """Most recent anchor-layer route error, if an anchor matched a layer."""

        return self._stationary_route_validation_error

    def set_stationary_pickup_schedule(
        self, enabled: bool, interval_seconds: float, jitter_seconds: float,
    ) -> None:
        """Configure the optional station-point pickup circuit live.

        ``interval_seconds`` and ``jitter_seconds`` stay seconds here (they are
        the configuration's own keys); the 捡东西 row presents the interval in
        minutes and converts before publishing.  Both bounds are enforced so a
        hand-edited configuration cannot schedule a circuit every few seconds.
        """

        self.stationary_pickup_enabled = bool(enabled)
        self.stationary_pickup_interval_seconds = min(
            STATIONARY_PICKUP_MAX_INTERVAL_SECONDS,
            max(STATIONARY_PICKUP_MIN_INTERVAL_SECONDS, float(interval_seconds)),
        )
        self.stationary_pickup_jitter_seconds = max(0.0, float(jitter_seconds))
        self._stationary_pickup_phase = None
        self._stationary_pickup_failures = 0
        self._stationary_pickup_retry_after_return = False
        self._stationary_pickup_return_active = False
        self._stationary_return_dismount_direction = None
        self._stationary_pickup_next_at = (
            time.monotonic() + self._stationary_pickup_delay()
            if self.stationary_pickup_enabled else float("inf")
        )
        LOG.info(
            "stationary pickup circuit %s interval=%.1fm random_gap=%.1fs",
            "enabled" if self.stationary_pickup_enabled else "disabled",
            self.stationary_pickup_interval_seconds / 60.0,
            self.stationary_pickup_jitter_seconds,
        )

    def _stationary_pickup_delay(self) -> float:
        return self.stationary_pickup_interval_seconds + random.uniform(
            0.0, self.stationary_pickup_jitter_seconds
        )

    def _stationary_pickup_decision(
        self, anchor: Point, player: Point,
    ) -> Optional[MovementDecision]:
        """Run one stationary pickup circuit without changing the anchor.

        The normal Z-with-walk path collects items.  This state machine only
        supplies its three horizontal destinations: left edge, right edge,
        then the temporary attack point.
        """

        if (not self.stationary_pickup_enabled
                or not self._stationary_return_route_ready
                or not self._stationary_route_anchor_layer):
            return None
        layer = self.important_positions.get(self._stationary_route_anchor_layer)
        if not isinstance(layer, dict):
            return None
        left, right = layer.get("left_most_pos"), layer.get("right_most_pos")
        if not (isinstance(left, dict) and isinstance(right, dict)):
            return None
        try:
            left_x, right_x = float(left["x"]), float(right["x"])
        except (KeyError, TypeError, ValueError):
            return None
        now = time.monotonic()
        if self._stationary_pickup_phase is None:
            if now < self._stationary_pickup_next_at:
                return None
            self._stationary_pickup_phase = "left"
            # Each pickup circuit is a new directional traversal.  Its jump
            # points must be available again even if the previous circuit
            # already crossed the same point before returning or falling.
            self._begin_jump_point_pass("stationary pickup circuit")
            self._jump_point_inside_tokens.clear()
            self._jump_point_suppressed_tokens.clear()
            self._jump_point_candidate = None
            LOG.info("STATIONARY PICKUP: starting %s left -> right -> anchor",
                     self._stationary_route_anchor_layer)
        phase = self._stationary_pickup_phase
        target = left_x if phase == "left" else right_x if phase == "right" else anchor.x
        if abs(target - player.x) <= self._current_horizontal_tolerance:
            if phase == "left":
                self._stationary_pickup_phase = "right"
                LOG.info("STATIONARY PICKUP: left edge reached; crossing right")
                target = right_x
            elif phase == "right":
                self._stationary_pickup_phase = "anchor"
                LOG.info("STATIONARY PICKUP: right edge reached; returning to anchor")
                target = anchor.x
            else:
                self._stationary_pickup_phase = None
                self._stationary_pickup_failures = 0
                self._stationary_pickup_next_at = now + self._stationary_pickup_delay()
                LOG.info("STATIONARY PICKUP: anchor restored; next run scheduled")
                return None
        direction = "right" if target > player.x else "left"
        return MovementDecision(
            direction,
            f"stationary pickup {self._stationary_pickup_phase} {direction}",
            self.movement_hold_seconds,
        )

    def _abort_stationary_pickup_for_route_return(self) -> None:
        """Discard every pickup plan before a confirmed fall returns to 桩.

        A drop invalidates both an in-progress left/right leg and a pickup
        timer that happened to be due.  Return-to-stake is therefore the sole
        owner until the anchor has been reached and a fresh interval is armed.
        """

        phase = self._stationary_pickup_phase
        self._stationary_pickup_phase = None
        self._stationary_pickup_return_active = True
        # A pickup jump can leave Up held for a rope.  This failed pickup is
        # now handing ownership to the route-return climb, so remove that
        # previous jump session before the return state decides its own Up.
        if self._jump_point_up_held:
            key_up = getattr(self.key_sender, "key_up", None)
            if callable(key_up):
                key_up("up")
            self._jump_point_up_held = False
            self._jump_point_up_started_at = 0.0
            self._jump_point_y_samples.clear()
            LOG.info("STATIONARY PICKUP: released jump-point Up for route return")
        self._stationary_pickup_failures = 0
        self._stationary_pickup_retry_after_return = False
        self._stationary_pickup_next_at = float("inf")
        LOG.warning(
            "STATIONARY PICKUP: cleared %s plan after leaving patrol route; "
            "returning to 桩 before a fresh interval is armed",
            phase or "pending",
        )

    def set_stationary_facing_direction(self, direction: str) -> None:
        """Apply a 朝向 selection and restart 双向 from the right side."""

        direction = str(direction).casefold()
        if direction not in ("left", "right", "both"):
            direction = "right"
        if self.stationary_facing_direction == direction:
            return
        self.stationary_facing_direction = direction
        self._stationary_bilateral_target = "right"
        self._stationary_bilateral_frames = 0
        # A changed selection is owed even if the character had already been
        # corrected for its old selection. The next settled frame queues the
        # new target through the arbiter.
        self._stationary_facing_command = None
        LOG.info("stationary facing selection changed to %s", direction)

    def stationary_attack_anchor_position(self) -> Optional[Point]:
        """The temporary 站桩攻击 anchor of this session, or None.

        None means no MANUAL Start Patrol has recorded a standing spot yet
        (the mode was selected while patrol was already running), so the
        position recovery has nothing to hold the character on.
        """

        return self._stationary_attack_anchor

    def prepare_stationary_attack_anchor(
        self, marker: Optional[Point], *, allow_reanchor: bool = True
    ) -> bool:
        """Record the temporary 站桩攻击 standing position.

        Only a MANUAL Start Patrol - the UI button or the Ctrl+` patrol
        toggle, which both ride ``UiWorker._start_patrol`` - may move the
        standing spot, so it always calls this with ``allow_reanchor=True``.
        An AUTOMATIC resume (the auto-lie pass pauses patrol for the Cutie
        takeover and resumes it afterwards) calls it with
        ``allow_reanchor=False``: the anchor the user's own start recorded is
        kept, so a knock-down or a drift during the pause can never move the
        spot the character must stand on.  With no anchor yet nothing is
        recorded (the character stands and attacks until the next manual
        Start Patrol).
        """

        if not self.stationary_attack_enabled:
            return True
        if not allow_reanchor:
            anchor = self._stationary_attack_anchor
            if anchor is not None:
                LOG.info(
                    "STATIONARY ATTACK automatic resume: keeping the temporary "
                    "anchor recorded by Start Patrol x=%.6f y=%.6f",
                    anchor.x, anchor.y,
                )
            else:
                LOG.warning(
                    "STATIONARY ATTACK automatic resume: no temporary anchor "
                    "was recorded yet; standing still until a manual Start "
                    "Patrol (按钮 / Ctrl+`) records one"
                )
            return True
        if marker is None:
            LOG.warning("stationary attack start rejected: yellow marker missing")
            return False

        # A button/Ctrl+` start is a new standing session, not a continuation
        # of the previous one.  The old temporary point may have borrowed a
        # recorded route for a pickup circuit or a return climb.  Reusing its
        # return phase here made a second manual start walk toward that old
        # route's final layer before honoring the freshly recorded point.
        #
        # Do not touch the recorded map configuration: only clear transient
        # movement state.  ``allow_reanchor=False`` is the automatic resume
        # path and deliberately skips this block so a reconnect/auto-lie pass
        # can still return to the existing temporary point.
        self._release_climb_up()
        self._release_walk_hold()
        self._climb_state = ClimbState()
        self._return_mode = None
        self._return_from_floor = None
        self._return_arrival_floor = None
        self._descending_to_first = False
        self._descending_since = None
        self._drop_descent_saw = None
        self._route_layer_index = None
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._forced_phase_entry = None
        self._held_rope_target = None
        self._climb_direction_log = None
        self._clear_layer_resync_candidate()
        self._reset_fall_tracking()
        with self._patrol_start_lock:
            self._pending_patrol_start_floor = None
            self._pending_patrol_start_above_route = False

        self._stationary_attack_anchor = Point(float(marker.x), float(marker.y))
        self._stationary_near_correction_next_at = 0.0
        self._stationary_step_target_x = None
        self._stationary_facing_confirm_frames_remaining = 0
        self._stationary_ui_anchor_layer = None
        self._stationary_route_anchor_layer = None
        self._stationary_return_route_ready = False
        self._stationary_pickup_phase = None
        self._stationary_pickup_failures = 0
        self._stationary_pickup_retry_after_return = False
        self._stationary_pickup_return_active = False
        self._stationary_return_dismount_direction = None
        # Restart the optional pickup timer for this new manual session.  A
        # timer that was already due in the previous run must not immediately
        # launch a left -> right -> anchor traversal on this fresh start.
        self._stationary_pickup_next_at = (
            time.monotonic() + self._stationary_pickup_delay()
            if self.stationary_pickup_enabled else float("inf")
        )
        # Recorded jump points are allowed once per movement pass, never once
        # per application lifetime.  A manual Start Patrol begins a new pass;
        # retaining these tokens made a point which fired on the first run
        # silently disappear on the second run.
        self._begin_jump_point_pass("stationary attack manual start")
        self._jump_point_inside_tokens.clear()
        self._jump_point_suppressed_tokens.clear()
        self._jump_point_candidate = None
        self._jump_point_up_held = False
        self._jump_point_up_started_at = 0.0
        self._jump_point_y_samples.clear()
        # The recorded position IS the anchor, so the X side starts settled.
        # Starting patrol must not send a direction merely to restore 朝向: that
        # visible tap nudges the character away from the just-recorded point.
        # Treat the current facing as settled until a genuine recovery walk or
        # a later 朝向 selection creates a new facing obligation.
        self._stationary_facing_command = self._stationary_facing_target_for_frame()
        self._stationary_bilateral_target = "right"
        self._stationary_bilateral_frames = 0
        self._stationary_x_settled = True
        self._reset_stationary_y_recovery()
        LOG.info(
            "STATIONARY ATTACK manual start: cleared prior runtime route and "
            "pickup state; automatic resumes retain their existing session"
        )
        LOG.info(
            "STATIONARY ATTACK temporary anchor saved x=%.6f y=%.6f "
            "x_zone=facing-biased +/-%.6f (shift %.6f) x_step_zone=+/-%.6f y_zone=+/-%.6f "
            "(not written to map recording)",
            marker.x, marker.y,
            STATIONARY_ATTACK_X_TOLERANCE, STATIONARY_ATTACK_FACING_ZONE_SHIFT,
            STATIONARY_ATTACK_X_HOLD_TOLERANCE,
            STATIONARY_ATTACK_Y_TOLERANCE,
        )
        return True

    def _reset_stationary_y_recovery(self) -> None:
        """Forget this displacement episode's Y-recovery jump bookkeeping."""

        self._stationary_y_jumps = 0
        self._stationary_y_jump_at = float("-inf")
        self._stationary_y_backoff_logged = False
        self._stationary_y_mismatch_sign = 0
        self._stationary_y_mismatch_frames = 0

    def _clear_stationary_route_state(self) -> None:
        """Drop route/vertical state that 站桩攻击 must never carry.

        站桩攻击 owns no route: a climb or return-to-route state latched before
        or during the mode (a knock-down fall is the observed cause) would
        block every attack beat - the field log showed
        ``attack skipped: climb/return input is active`` while the character
        only stood at its anchor - and a stale climb state can even hold Up.
        """

        if (self._climb_state.up_held or self._climb_state.phase != "idle"):
            LOG.warning(
                "STATIONARY ATTACK: releasing a latched climb state "
                "(phase=%s up_held=%s) - 站桩攻击 never climbs",
                self._climb_state.phase,
                self._climb_state.up_held,
            )
            self._release_climb_up()
            self._climb_state = ClimbState()
        if self._return_mode is not None or self._descending_to_first:
            LOG.warning(
                "STATIONARY ATTACK: clearing the latched route vertical state "
                "(%s) - 站桩攻击 has no route",
                self._return_mode or "descending-to-first",
            )
            self._return_mode = None
            self._return_from_floor = None
            self._return_arrival_floor = None
            self._descending_to_first = False

    def _stationary_y_recovery_decision(
        self, anchor: Point, player: Point
    ) -> MovementDecision:
        """Keep jumping while the standing Y is wrong.

        X is recovered by walking and comes first.  Y cannot be walked back:
        it needs the Alt jump.  The marker-Y jitter band decides whether the
        character is displaced at all, and the attempt re-arms while the
        mismatch lasts - one jump per `STATIONARY_ATTACK_Y_JUMP_GAP_SECONDS`,
        slowing to `STATIONARY_ATTACK_Y_RETRY_SECONDS` after the first burst -
        so a character that really is below its spot (a jump cannot climb
        back) never jumps forever.
        """

        gap_y = anchor.y - player.y
        if abs(gap_y) <= STATIONARY_ATTACK_Y_TOLERANCE:
            if self._stationary_y_jumps:
                LOG.info(
                    "stationary Y recovery: back on the launch position "
                    "(y=%.6f anchor_y=%.6f after %d jumps)",
                    player.y, anchor.y, self._stationary_y_jumps,
                )
            self._reset_stationary_y_recovery()
            return MovementDecision(
                None, "stationary attack temporary safe zone"
            )

        # A stationary-recovery jump is only useful while the marker remains
        # on the anchor platform.  Once it is outside this local same-layer
        # band, a jump cannot recover a fall and must not compete with the
        # ordinary route-return logic.
        if abs(gap_y) > STATIONARY_ATTACK_SAME_LAYER_Y_TOLERANCE:
            self._reset_stationary_y_recovery()
            return MovementDecision(
                None,
                "stationary Y recovery skipped: marker outside anchor layer band",
            )

        # Do not turn a one-frame marker flicker into Alt.  In the supplied
        # field log, y=0.503571 appeared once beside the real anchor
        # y=0.510714 (exactly one minimap pixel), which launched a needless
        # recovery jump and the following fall/route-return sequence.
        mismatch_sign = 1 if gap_y > 0.0 else -1
        if mismatch_sign == self._stationary_y_mismatch_sign:
            self._stationary_y_mismatch_frames += 1
        else:
            self._stationary_y_mismatch_sign = mismatch_sign
            self._stationary_y_mismatch_frames = 1
        if self._stationary_y_mismatch_frames < STATIONARY_ATTACK_Y_CONFIRM_FRAMES:
            return MovementDecision(
                None,
                "stationary Y recovery: confirming displaced marker "
                f"({self._stationary_y_mismatch_frames}/"
                f"{STATIONARY_ATTACK_Y_CONFIRM_FRAMES})",
            )
        now = time.monotonic()
        burst = self._stationary_y_jumps < STATIONARY_ATTACK_Y_BURST_JUMPS
        wait_seconds = (
            STATIONARY_ATTACK_Y_JUMP_GAP_SECONDS if burst
            else STATIONARY_ATTACK_Y_RETRY_SECONDS
        )
        elapsed = now - self._stationary_y_jump_at
        if elapsed < wait_seconds:
            return MovementDecision(
                None,
                f"stationary Y recovery: waiting {wait_seconds - elapsed:.1f}s "
                f"for the next jump (y={player.y:.6f} anchor_y={anchor.y:.6f})",
            )
        if not burst and not self._stationary_y_backoff_logged:
            self._stationary_y_backoff_logged = True
            LOG.warning(
                "STATIONARY ATTACK Y recovery: %d jumps did not restore "
                "anchor_y=%.6f (player_y=%.6f); retrying one jump every %.1fs",
                self._stationary_y_jumps, anchor.y, player.y,
                STATIONARY_ATTACK_Y_RETRY_SECONDS,
            )
        self._stationary_y_jumps += 1
        self._stationary_y_jump_at = now
        LOG.info(
            "STATIONARY ATTACK Y recovery jump %d: player_y=%.6f anchor_y=%.6f "
            "gap_y=%+.6f",
            self._stationary_y_jumps, player.y, anchor.y, gap_y,
        )
        return MovementDecision(
            "stationary_jump",
            f"stationary Y recovery jump {self._stationary_y_jumps} "
            f"(player_y={player.y:.6f} anchor_y={anchor.y:.6f})",
            self.minimum_final_hold_seconds,
        )

    def _request_stationary_step(
        self, direction: str, *, target_x: Optional[float] = None,
    ) -> bool:
        """Queue one tiny 桩 step.

        Only one step may be in flight: the marker moves by about a pixel per
        step, so two queued steps would walk in opposite directions and net out.
        While one is queued or running this returns False and the movement loop
        keeps the correction owed - the next capture either re-asks or observes
        the anchor as reached.
        """

        arbiter = self.motion_arbiter
        if arbiter is None:
            return False
        pending = getattr(arbiter, "step_pending", None)
        if callable(pending) and pending():
            return False
        request = getattr(arbiter, "request_step", None)
        if not callable(request):
            return False
        self._stationary_step_target_x = (
            None if target_x is None else float(target_x)
        )
        accepted = bool(request(direction))
        if not accepted:
            self._stationary_step_target_x = None
        return accepted

    def _stationary_attack_decision(
        self, observation: MinimapObservation
    ) -> MovementDecision:
        """Return recovery against this run's temporary start-position anchor."""

        anchor = self._stationary_attack_anchor
        player = observation.player
        if anchor is None:
            return MovementDecision(None, "stationary attack awaiting start position")
        if player is None:
            return MovementDecision(None, "stationary attack waiting for marker")
        dismount_direction = self._stationary_return_dismount_direction
        if dismount_direction in ("left", "right"):
            # The preceding return climb reached its layer band but can still
            # leave the marker attached at the rope lip.  Consume this once,
            # before a pickup retry or ordinary anchor correction can flood
            # the rope with tiny holds that have no game-side effect.
            self._stationary_return_dismount_direction = None
            self._stationary_x_settled = False
            LOG.info(
                "STATIONARY RETURN: stepping %s off rope toward 桩 (hold %.2fs)",
                dismount_direction, STATIONARY_RETURN_ROPE_DISMOUNT_HOLD_SECONDS,
            )
            return MovementDecision(
                dismount_direction,
                "stationary return rope dismount",
                STATIONARY_RETURN_ROPE_DISMOUNT_HOLD_SECONDS,
            )
        gap_x = anchor.x - player.x
        distance_x = abs(gap_x)
        # 捡东西 owns the whole left -> right -> anchor circuit.  Do this
        # before normal anchor recovery so crossing either side cannot be
        # pulled back to 桩 mid-run.  A wrong-Y observation still goes through
        # the vertical recovery below instead of walking a pickup route on a
        # different platform.
        # Once a pickup leg has started it owns movement regardless of a
        # transient marker-Y change (jump arc, platform edge, or minimap
        # jitter).  The route detector aborts it as soon as a real fall onto
        # another layer is confirmed.  Restrict the Y check only when *first*
        # starting a new pickup round, otherwise the normal 桩 return takes
        # over mid-leg the moment the marker leaves the exact launch Y.
        pickup_is_active = self._stationary_pickup_phase is not None
        # A freshly due pickup may only start from a confirmed, settled 桩.
        # A fall used to retain the previous settled-X flag for a few frames;
        # if its minimap Y happened to resemble the anchor row, the pickup
        # circuit could steal movement before route return had started.
        pickup_may_start = self._stationary_x_settled
        if (pickup_is_active
                or (pickup_may_start
                    and abs(anchor.y - player.y) <= STATIONARY_ATTACK_Y_TOLERANCE)):
            pickup = self._stationary_pickup_decision(anchor, player)
            if pickup is not None:
                return pickup
        # A recovery walk faces toward the anchor.  When a final facing is
        # still owed, approach the opposite inner edge of the accepted zone
        # first, then make the short facing tap back into that zone.  For
        # example, a left-facing character returning from the left walks to
        # the right-side turn point, then taps Left.  Turning at the exact
        # anchor was the source of the face/walk loop: the tap could nudge the
        # marker over the wrong edge and immediately demand an opposite walk.
        desired_facing = self._stationary_facing_target_for_frame()
        facing_owed = self._stationary_facing_command != desired_facing
        correction_target_x = (
            self._stationary_facing_turn_x(anchor.x, desired_facing)
            if facing_owed else anchor.x
        )
        position_at_turn_point = self._stationary_position_at_target(
            player.x, correction_target_x,
        )
        # The target remains the exact temporary anchor once facing is
        # satisfied, while its accepted resting window is shifted slightly
        # toward the selected facing.  A character outside that window still
        # finishes its arbiter-owned correction.
        # A running 小碎步 owns the position AND the facing for its whole pair:
        # it steps away and back on purpose, so its own step must not be
        # answered by a correction step (with the attack that correction now
        # carries) - that was the extra step the operator saw right after
        # 小碎步.  Outside that window the character must ALWAYS walk back to the
        # 桩: a tolerance band that "holds" a small drift left it standing a
        # pixel or two off the stake, which the operator rejected.
        micro_step_in_flight = self._micro_step_in_flight()
        if ((not facing_owed and self._stationary_anchor_x_accepted(anchor.x, player.x))
                or (facing_owed and position_at_turn_point)
                or micro_step_in_flight):
            # Arrived: the anchor band is reached, so the position is settled
            # from here on.
            self._stationary_x_settled = not bool(
                self._stationary_facing_confirm_frames_remaining
            )
            self._stationary_near_correction_next_at = 0.0
        else:
            # Outside the final approach zone, walk normally and do not
            # attack.  A layer-return can reach the anchor platform at its
            # rope top, far from the temporary stake; treating that distance
            # as a stream of tiny step-plus-attack motions prevented it from
            # ever walking across the layer.  Only the last +/-0.02 X uses
            # the short, attack-carrying correction below.
            self._stationary_x_settled = False
            # When a final facing is owed, the correction is aimed at its
            # opposite-side turn point rather than necessarily at X itself.
            # Use that live goal for direction: a marker already just to one
            # side of X can otherwise be walked the wrong way before turning.
            correction_gap_x = correction_target_x - player.x
            direction = "right" if correction_gap_x > 0 else "left"
            if distance_x > STATIONARY_ATTACK_FINAL_APPROACH_X_RANGE:
                self._stationary_near_correction_next_at = 0.0
                return MovementDecision(
                    direction,
                    "stationary return walking to final approach zone",
                    self.movement_hold_seconds,
                )
            now = time.monotonic()
            if now < self._stationary_near_correction_next_at:
                return MovementDecision(
                    None, "stationary X correction settling"
                )
            if not self._request_stationary_step(
                    direction, target_x=correction_target_x):
                # Refused or not yet available (a correction still in flight,
                # gate shut, focus dip).  Retry on the next capture instead of
                # waiting out the correction interval: the correction is an
                # OBLIGATION while the marker is off the band, and a refused
                # request must not look like a settled position.
                return MovementDecision(
                    None,
                    f"stationary X correction {direction} not queued",
                )
            self._stationary_near_correction_next_at = (
                now + STATIONARY_ATTACK_NEAR_CORRECTION_INTERVAL_SECONDS
            )
            return MovementDecision(
                None,
                f"stationary X correction {direction} queued",
            )
        # X is settled.  The standing Y is this session's launch position, not a
        # recorded map layer.
        if self._stationary_facing_confirm_frames_remaining:
            self._stationary_facing_confirm_frames_remaining -= 1
            if self._stationary_facing_confirm_frames_remaining > 0:
                self._stationary_x_settled = False
                return MovementDecision(None, "stationary facing settling")
            self._stationary_x_settled = True
            LOG.info("stationary facing confirmed by fresh marker reads")
        decision = self._stationary_y_recovery_decision(anchor, player)
        if decision.key is not None:
            # A recovery jump owns this frame; turn the character afterwards.
            return decision
        # The facing is applied even while the Y side waits for its next jump:
        # a jump does not change the facing, and holding the 朝向 hostage to a
        # spot the jump cannot reach is how the correction disappeared.
        return self._stationary_facing_decision(decision)

    def _stationary_facing_decision(
        self, settled_decision: MovementDecision
    ) -> MovementDecision:
        """Apply the selected 朝向 as the last atomic motion on a settled anchor.

        The operator selects the side the character must face while it stands
        and attacks (朝向).  A position recovery walks the character back to
        the anchor, and that walk leaves it facing the way it came - the
        observed field fault: 朝向左 selected, a monster knocks the character
        to the left, the recovery walks right, and the character keeps standing
        on its spot facing right.

        The correction therefore rides the same atomic arbiter path as the
        small-step, and it stays OWED until the arbiter reports the key really
        went down: a queued token can be drained while a walk handoff or a
        focus dip shuts the arbiter's safe-stage gate, and a dropped token must
        never be mistaken for an applied facing.  Whether a token is still in
        flight is asked of the arbiter itself, so a drained request is re-queued
        on the next settled capture instead of latching the correction away.
        """

        direction = self._stationary_facing_target_for_frame(advance=True)
        if direction not in ("left", "right"):
            return settled_decision
        if self._stationary_facing_command == direction:
            return settled_decision
        if self._micro_step_in_flight():
            # The 小碎步 pair ends facing the selected 朝向 itself and records
            # that when it completes, so a tap queued now would land as an extra
            # step right after the pair.  The obligation stays owed: the next
            # settled capture after the pair re-asks if it is still unmet.
            return MovementDecision(
                None,
                f"stationary facing {direction} waiting for 小碎步 to finish",
            )
        pending = getattr(self.motion_arbiter, "facing_pending", None)
        if callable(pending) and pending(direction):
            return MovementDecision(
                None, f"stationary facing {direction} queued",
            )
        request = getattr(self.motion_arbiter, "request_facing", None)
        if not callable(request):
            return settled_decision
        if self._walk_hold_key is not None:
            # A direction key is still held from the recovery walk: the tap
            # would be swallowed by that walk, or undone by its remainder.  The
            # frame that decides "wait" releases the hold, so the next capture
            # queues the correction.
            return MovementDecision(
                None,
                f"stationary facing {direction} waiting for the walk to settle",
            )
        if request(direction):
            return MovementDecision(
                None, f"stationary facing {direction} queued",
            )
        return MovementDecision(
            None, f"stationary facing {direction} refused; will retry",
        )

    def _stationary_facing_target_for_frame(self, *, advance: bool = False) -> str:
        """Return the selected stationary facing for this settled frame.

        双向 does not issue raw directional input from the movement loop.  It
        merely changes the target after 80 settled minimap frames; the normal
        facing obligation below then queues one atomic arbiter correction.
        This preserves the same attack/movement exclusion as 左 and 右.
        """

        setting = str(self.stationary_facing_direction).casefold()
        if setting in ("left", "right"):
            self._stationary_bilateral_target = setting
            self._stationary_bilateral_frames = 0
            return setting
        if setting != "both":
            return "right"
        if advance:
            self._stationary_bilateral_frames += 1
        if (advance and self._stationary_bilateral_frames
                >= STATIONARY_ATTACK_BILATERAL_FACING_FRAMES):
            self._stationary_bilateral_frames = 0
            previous = self._stationary_bilateral_target
            self._stationary_bilateral_target = (
                "left" if previous == "right" else "right"
            )
            LOG.info(
                "stationary bilateral facing toggled: %s -> %s after %d frames",
                previous,
                self._stationary_bilateral_target,
                STATIONARY_ATTACK_BILATERAL_FACING_FRAMES,
            )
        return self._stationary_bilateral_target

    def perform_stationary_facing(self, direction: str) -> bool:
        """Atomically turn the character to the selected 朝向.

        Called only by ``MotionArbiter``, so attacks and other queued motions
        are already excluded and the movement loop's direction lock keeps the
        ordinary walk out of the tap.  It returns False (the arbiter then drains
        the token) whenever the character is not in a state where the tap can
        stick; movement keeps the facing owed and queues it again.
        """

        direction = str(direction).casefold()
        if (not self._patrol_input_allowed()
                or direction not in ("left", "right")
                or not _sender_is_safe(self.key_sender)):
            return False
        if not self.stationary_attack_enabled or self._movement_busy_now():
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            return False
        with self._direction_lock, self._hold_lock:
            if (not self._patrol_input_allowed()
                    or not self.stationary_attack_enabled
                    or self._movement_busy_now()):
                return False
            if self._walk_hold_key not in (None, direction):
                # The position recovery is still travelling the other way; a
                # tap now would be undone by the rest of that walk.
                LOG.info(
                    "stationary facing %s deferred: still walking %s",
                    direction, self._walk_hold_key,
                )
                return False
            self._release_walk_hold()
            if key_down(direction) is False:
                return False
            try:
                if not self._wait_for_patrol_motion(
                        STATIONARY_ATTACK_FACING_HOLD_SECONDS):
                    return False
            finally:
                key_up(direction)
        # The character faces the selected side now.  The obligation is cleared
        # only here, after the key really went down and up.  Its small movement
        # is deliberately NOT accepted as settled yet: two fresh marker reads
        # must prove it remained inside the standing band before the normal
        # attack cadence may resume.
        self._stationary_facing_command = direction
        self._stationary_x_settled = False
        self._stationary_facing_confirm_frames_remaining = (
            STATIONARY_ATTACK_FACING_CONFIRM_FRAMES
        )
        self._patrol_facing = direction
        LOG.info(
            "stationary facing correction executed: %s (hold %.2fs)",
            direction, STATIONARY_ATTACK_FACING_HOLD_SECONDS,
        )
        return True

    def _micro_step_in_flight(self) -> bool:
        """Whether the optional 小碎步 pair is queued or running.

        The pair is a deliberate two-direction motion that ends facing the
        selected 朝向, so while it runs neither the position correction nor the
        朝向 tap may be queued behind it: answering the pair's own step with a
        step of our own is what the operator saw as an extra step after 小碎步.
        """

        arbiter = getattr(self, "motion_arbiter", None)
        pending = getattr(arbiter, "micro_step_pending", None)
        if not callable(pending):
            return False
        try:
            return bool(pending())
        except Exception:
            LOG.exception("could not ask the arbiter about a running 小碎步")
            return False

    def _stationary_step_still_needed(self, direction: str) -> bool:
        """Whether the live marker still needs a step in *direction* to the 桩.

        A queued step can outlive the gap that asked for it: the character may
        have been pushed back onto the anchor, or a facing tap may have arrived
        first.  Stepping then would push it off the anchor again, so the step is
        dropped and the arbiter drains the token.
        """

        anchor = self._stationary_attack_anchor
        observation = self.last_observation
        player = getattr(observation, "player", None)
        if anchor is None or player is None:
            return False
        target_x = self._stationary_step_target_x
        if target_x is None:
            target_x = anchor.x
        gap_x = float(target_x) - player.x
        if self._stationary_position_at_target(player.x, float(target_x)):
            return False
        return ("right" if gap_x > 0 else "left") == direction

    @staticmethod
    def _stationary_position_at_target(player_x: float, target_x: float) -> bool:
        """Whether the marker is close enough to the current finite step goal."""

        # One minimap pixel is commonly around 0.008 X.  A step is only 30ms,
        # so its goal must tolerate a fraction of that pixel or it will ping-
        # pong across the intended turning side.
        return abs(float(player_x) - float(target_x)) <= 0.003

    @staticmethod
    def _stationary_facing_turn_x(anchor_x: float, direction: str) -> float:
        """Return the inner opposite-side turning point for a final facing tap."""

        direction = str(direction).casefold()
        shift = (
            -STATIONARY_ATTACK_FACING_ZONE_SHIFT
            if direction == "left" else STATIONARY_ATTACK_FACING_ZONE_SHIFT
        )
        edge = (
            float(anchor_x) + STATIONARY_ATTACK_X_TOLERANCE + shift
            if direction == "left"
            else float(anchor_x) - STATIONARY_ATTACK_X_TOLERANCE + shift
        )
        return (
            edge - STATIONARY_ATTACK_FACING_TURN_INSET
            if direction == "left"
            else edge + STATIONARY_ATTACK_FACING_TURN_INSET
        )

    def _stationary_anchor_x_accepted(self, anchor_x: float, player_x: float) -> bool:
        """Whether X is in the face-biased resting zone around the stake.

        The correction target is always ``anchor_x``.  Only the acceptance
        window moves: left-facing uses ``[X-0.010, X+0.006]`` and right-facing
        uses ``[X-0.006, X+0.010]``.  This absorbs the known facing-tap nudge
        without making either edge a movement target.
        """

        facing = self._stationary_facing_target_for_frame()
        shift = (
            -STATIONARY_ATTACK_FACING_ZONE_SHIFT
            if facing == "left" else STATIONARY_ATTACK_FACING_ZONE_SHIFT
        )
        lower = float(anchor_x) - STATIONARY_ATTACK_X_TOLERANCE + shift
        upper = float(anchor_x) + STATIONARY_ATTACK_X_TOLERANCE + shift
        return lower <= float(player_x) <= upper

    def _stationary_attack_ready_for_player(
        self, anchor: Point, player: Point,
    ) -> bool:
        """Whether the independent fixed cadence may safely resume.

        Final recovery owns attacks through its arbiter STEP motions.  The
        independent cadence must remain quiet until the marker, target facing,
        and post-facing confirmation all agree; otherwise it races the final
        tiny correction and can make the character appear frozen.
        """

        if (not self._stationary_x_settled
                or self._stationary_facing_confirm_frames_remaining > 0):
            return False
        if not self._stationary_anchor_x_accepted(anchor.x, player.x):
            return False
        return (
            self._stationary_facing_command
            == self._stationary_facing_target_for_frame()
        )

    def _stationary_correction_hold(self) -> float:
        """How long the anchor correction holds its direction for the live gap.

        Inside the small-step band the correction is the operator's tiny step;
        further out the same motion becomes the longer walk back.  Both carry
        the attack that belongs to the correction (see
        ``perform_stationary_step``), so the hold is the only thing that differs.
        """

        anchor = self._stationary_attack_anchor
        player = getattr(getattr(self, "last_observation", None), "player", None)
        if anchor is None or player is None:
            return STATIONARY_ATTACK_NEAR_RECOVERY_HOLD_SECONDS
        if abs(anchor.x - player.x) > STATIONARY_ATTACK_X_HOLD_TOLERANCE:
            return STATIONARY_ATTACK_RECOVERY_HOLD_SECONDS
        return STATIONARY_ATTACK_NEAR_RECOVERY_HOLD_SECONDS

    def perform_stationary_step(self, direction: str) -> bool:
        """Correct the marker toward the 桩, and attack as part of that motion.

        Called only by ``MotionArbiter``, so this is ONE finite motion: hold the
        direction for the distance-appropriate hold, release it, then tap the
        attack key.  That combination is the whole point - the field run showed
        the character correcting its position with no attack at all, because a
        correction sent as an ordinary walk hold either deferred the fixed
        cadence (direction handoff) or blocked it (exclusive recovery), and the
        beats that landed inside a correction were lost.  Carrying the attack
        inside the correction means no correction can ever eat one.

        The correction is dropped when the live marker no longer needs it;
        returning False then lets the arbiter drain the token and the movement
        loop asks again on the next capture while the marker stays off the band.
        """

        direction = str(direction).casefold()
        if (not self._patrol_input_allowed()
                or direction not in ("left", "right")
                or not _sender_is_safe(self.key_sender)):
            return False
        if not self.stationary_attack_enabled or self._movement_busy_now():
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        tap = getattr(self.key_sender, "tap", None)
        if key_down is None or key_up is None or not callable(tap):
            return False
        hold = self._stationary_correction_hold()
        with self._direction_lock, self._hold_lock:
            if (not self._patrol_input_allowed()
                    or not self.stationary_attack_enabled
                    or self._movement_busy_now()):
                return False
            if self._walk_hold_key not in (None, direction):
                # An ordinary walk is still travelling the other way; a
                # correction now would be undone by the rest of that walk.
                LOG.info(
                    "stationary correction %s deferred: still walking %s",
                    direction, self._walk_hold_key,
                )
                return False
            if not self._stationary_step_still_needed(direction):
                self._stationary_step_target_x = None
                return False
            # The finite target has served its purpose.  A following capture
            # decides whether another correction is needed; it must not reuse
            # a turn point from this already-running motion.
            self._stationary_step_target_x = None
            self._release_walk_hold()
            if key_down(direction) is False:
                return False
            try:
                if not self._wait_for_patrol_motion(hold):
                    return False
            finally:
                key_up(direction)
            # The step turned the character, exactly like the ordinary recovery
            # walk this replaces (see the stationary bookkeeping in
            # _send_walk_hold).
            self._stationary_facing_command = direction
            self._patrol_facing = direction
            # The attack half of the combination, sent right after the released
            # direction (the same order the 小碎步 uses for its middle attack).
            # The UI publishes the configured fixed-attack key on
            # ``small_step_attack_key`` for these atomic step motions.
            attack_key = str(getattr(self, "small_step_attack_key", "ctrl"))
            if (not self._patrol_input_allowed()
                    or tap(attack_key) is False):
                LOG.warning(
                    "stationary correction %s: attack tap not delivered",
                    direction,
                )
                return False
            if self.motion_arbiter is not None:
                # Register the tap so the shared grace window knows an attack
                # animation is running: the next queued motion (a 朝向 tap, a
                # buff) waits it out instead of having its key swallowed.
                note_attack = getattr(self.motion_arbiter, "note_attack", None)
                if callable(note_attack):
                    note_attack()
        LOG.info(
            "stationary correction executed: %s (hold %.2fs) + attack %s",
            direction, hold, attack_key,
        )
        return True

    def _apply_pending_patrol_start(
        self, observation: MinimapObservation
    ) -> bool:
        """Begin a fresh patrol or return using Start Patrol's floor result.

        This runs on the movement thread, after the latest route snapshot was
        loaded, so resetting key/state ownership cannot race normal movement.
        """

        with self._patrol_start_lock:
            floor = self._pending_patrol_start_floor
            self._pending_patrol_start_floor = None
            above_route = self._pending_patrol_start_above_route
            self._pending_patrol_start_above_route = False
            reconnect_restart = self._pending_patrol_start_reconnect
            self._pending_patrol_start_reconnect = False
        if floor is None:
            return False

        # A manual start never inherits a reconnect fallback.  An automatic
        # reconnect start is the sole caller allowed to arm it.
        self._reconnect_drop_recovery_armed = bool(reconnect_restart)
        self._reconnect_drop_attempt_y = None
        self._reconnect_drop_attempt_at = float("-inf")
        self._reconnect_drop_assessed_at = float("-inf")
        self._reconnect_drop_stalled_attempts = 0
        self._reconnect_drop_edge_phase = None
        self._reconnect_drop_edge_started_at = 0.0
        self._reconnect_drop_edge_start_y = None
        # A fresh start owns a fresh descent: no landing evidence from the run
        # that just ended may prove this one has moved down.
        self._reset_drop_arrival()

        self._release_climb_up()
        self._release_walk_hold()
        # A fresh patrol start must not inherit a game-side stuck movement key
        # from the previous run (its key-up can be lost when patrol stopped):
        # release every movement key once before the new route begins.
        self._release_stuck_keys()
        self._climb_state = ClimbState()
        self._descending_to_first = False
        self._return_mode = None
        self._return_from_floor = None
        self._return_arrival_floor = None
        self._clear_layer_resync_candidate()
        self._fall_pending = False
        self._fall_floor_candidate = None
        self._fall_frames = 0
        self._fall_last_y = None
        self._fall_keys_released = False
        self._reset_fall_settle()
        self._forced_phase_entry = None
        self._aligned_frames = 0
        self._rope_approach_direction = None
        self._rope_attempted = False
        # A fresh start re-locks the rope target: the previous run's locked X
        # belongs to a route state that no longer exists.
        self._held_rope_target = None
        self._climb_direction_log = None
        self._route_patrol_cycle = 1
        self._last_drop_attempt = float("-inf")
        self._patrol_busy_until = 0.0
        # A Start Patrol is a new movement pass: every recorded jump point is
        # armed again (the operator's rule), whatever the previous run did.
        self._begin_jump_point_pass("start patrol")
        with self._stair_jump_completion_lock:
            self._stair_jump_completion = None
        self._stair_jump_skip_frames = 0
        self._reset_stair_state()
        for event in (
            self.climbing_active_event,
            self.stationary_recovery_active_event,
            self.dropping_active_event,
            self.near_rope_event,
            self.moving_active_event,
            self.direction_transition_event,
        ):
            if event is not None:
                event.clear()

        if above_route:
            # ``floor`` is the top route layer only as a stable world-Y
            # reference.  The live marker was above it, so never let that
            # fallback name start horizontal patrol.  Reuse the normal
            # Alt+Down return phase until a real marker reading enters the
            # patrol range.
            self._route_layer_index = None
            self._return_mode = "drop-to-route"
            self._return_from_floor = None
            LOG.warning(
                "PATROL START: marker is above the recorded patrol route; "
                "dropping until it enters the route"
            )
            return True

        if floor in self._route_layers:
            self._start_patrol_on(floor, observation)
            phases = self._layer_phases(floor)
            self._route_phase = phases[0] if phases else "stand"
            LOG.info(
                "PATROL START: detected %s in patrol range; starting at %s "
                "cycle 1/%d",
                floor, self._route_phase, self.patrol_cycles_per_layer,
            )
            return True

        self._route_layer_index = None
        number = _layer_number(floor)
        self._return_mode = (
            "climb-to-route" if number < self._patrol_range_min
            else "drop-to-route"
        )
        self._return_from_floor = floor
        self._reanchor_tracker_to_layer(floor, observation)
        LOG.warning(
            "PATROL START: detected %s outside patrol range; %s",
            floor,
            "climbing back to route"
            if self._return_mode == "climb-to-route"
            else "dropping back to route",
        )
        return True

    def _start_patrol_on(
        self, floor: str, observation: Optional[MinimapObservation] = None
    ) -> None:
        """Restart patrol from ``floor`` (must be inside the patrol range)."""
        self._clear_layer_resync_candidate()
        self._route_layer_index = self._route_layers.index(floor)
        # Back inside the patrol range: any below-range rescue streak is over.
        self._rescue_cycles = 0
        self._climb_lateral_streak = 0
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._climb_state = ClimbState()
        self._descending_to_first = False
        self._return_from_floor = None
        self._aligned_frames = 0
        self._rope_approach_direction = None
        self._rope_attempted = False
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.near_rope_event is not None:
            self.near_rope_event.clear()
        if self.direction_transition_event is not None:
            self.direction_transition_event.clear()
        self._reanchor_tracker_to_current_layer(observation)

    def _landing_floor_cap(self, marker_y: float) -> Optional[str]:
        """Highest recorded floor the minimap marker can still place the character on.

        A fall or a drop only ever moves the character DOWN, so a landing can never be a floor that the
        marker draws the character BELOW: the floor whose band lies entirely above the marker (band lower
        edge < marker Y) is out of reach for a character that is visibly under it.  This is the check that
        stops the world-Y tracker from answering with the floor the character fell FROM - the tracker
        lags a fast knock-down and then sits still at the old floor's anchor, which also satisfies the
        "settled" test in ``_reconcile_landed_floor`` (stable but wrong).  Operator report: the character
        dropped down but the patrol restarted on layer2 instead of the floor it was really standing on.

        When even the lowest floor's band lies above the marker (a landing in the pit under the map's
        last floor) the bottom floor is the cap: nothing is recorded below it.
        """

        floors = [
            name for name, layer in self.important_positions.items()
            if _has_layer_y_supporter(layer)
        ]
        if not floors:
            return None
        highest: Optional[str] = None
        for name in floors:
            layer = self.important_positions[name]
            band = _layer_y_band(layer, float(layer.get("y_tolerance", 0.020000)))
            if band is None or band[1] < marker_y - 1e-9:
                # The whole band sits above the marker position: the character cannot be standing here.
                continue
            if highest is None or _layer_number(name) > _layer_number(highest):
                highest = name
        if highest is None:
            return self._bottom_recorded_layer()
        return highest

    def _cap_landing_floor(
        self,
        floor: Optional[str],
        observation: Optional[MinimapObservation],
        *,
        source: str,
    ) -> Optional[str]:
        """Lower a resolved landing floor to what the marker allows (see ``_landing_floor_cap``).

        Only ever moves the answer DOWN a floor.  A floor that comes out one step too low is
        self-correcting (the return-to-route climb carries the character back up), while a floor one step
        too high leaves the patrol walking into empty space on a floor the character is not standing on -
        which is the failure the operator reported.  Returns ``floor`` unchanged when the two agree or
        when nothing is known.
        """

        if floor is None or observation is None or observation.player is None:
            return floor
        cap = self._landing_floor_cap(observation.player.y)
        if cap is None or _layer_number(floor) <= _layer_number(cap):
            return floor
        pair = (floor, cap)
        if pair not in _LANDING_CAP_REPORTED:
            _LANDING_CAP_REPORTED.add(pair)
            LOG.warning(
                "LANDING FLOOR CAP: %s reported %s, but the minimap marker at y=%.6f is at/below %s "
                "- a floor whose band lies entirely above the marker cannot be where the character "
                "stands, so the floor is %s",
                source, floor, observation.player.y, cap, cap,
            )
        return cap

    def _detect_floor_all(self, observation: MinimapObservation) -> Optional[str]:
        """Detect the floor over ALL recorded layers (not just the patrol
        range), so an out-of-range landing is recognized for the return."""
        layers = {
            name: layer for name, layer in self.important_positions.items()
            if _has_layer_y_supporter(layer)
        }
        if observation.player is not None:
            name = detect_layer_by_y(observation.player.y, layers)
            if name is not None:
                return name
        if (observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            world_layers = {
                name: layer for name, layer in layers.items()
                if isinstance(layer, dict) and "layer_world_y" in layer
            }
            world_name = detect_layer_by_world_y(
                observation.world_y_diamonds, world_layers
            )
            if world_name is not None:
                # A lagging world-Y tracker reports the floor the character came FROM.  The marker
                # position is the physical limit: the answer may not be a floor the character is
                # visibly standing below.
                return self._cap_landing_floor(
                    world_name, observation, source="the world-Y tracker"
                )
        # At/below the lowest recorded band: the character is on (or under)
        # the bottom floor - nothing lower exists.  This guarantees the
        # bottom floor is recognized even when its recorded band does not
        # cover the exact landing spot (e.g. a right-side knock-down into
        # the pit under a stair floor), so the return-to-route starts
        # instead of the stale route keeping the character walking.
        if observation.player is not None:
            bottom_floor = self._bottom_recorded_layer()
            bottom_layer = (
                self.important_positions.get(bottom_floor, {})
                if bottom_floor is not None else {}
            )
            band = (
                _layer_y_band(
                    bottom_layer,
                    float(bottom_layer.get("y_tolerance", 0.020000)),
                )
                if isinstance(bottom_layer, dict) else None
            )
            if band is not None and observation.player.y >= band[1] - 1e-9:
                return bottom_floor
        return None

    def _detect_stationary_return_floor(
        self,
        observation: MinimapObservation,
        *,
        after_confirmed_fall: bool = False,
    ) -> Optional[str]:
        """Resolve a floor for a stand-still return without hiding a fall.

        The ordinary resolver intentionally picks the closest recorded Y
        position.  That is right while the character is quietly standing on
        the temporary anchor.  It is not enough after a confirmed fall when
        two recorded layer bands overlap: the old anchor can still be a
        legal, closer Y match and therefore masks the lower landing.  In that
        one situation, a matching *other* layer is stronger evidence than the
        anchor and must start the existing route/climb recovery first.

        This does not relax the recording guard or invent a layer.  It only
        changes the tie-breaker after the fall detector has already proved a
        vertical displacement.
        """

        anchor_layer = self._stationary_route_anchor_layer
        if not anchor_layer or observation.player is None:
            return self._detect_floor_all(observation)

        anchor = self._stationary_attack_anchor
        # A temporary anchor can deliberately belong to an empty layer.  It
        # has no saved ``layer_y`` for the ordinary detector, but its current
        # minimap Y is still valid for confirming arrival after a rope climb.
        # This answers "am I still on the stake's physical platform?", not
        # "am I exactly at the temporary launch pixel?"  A monster hit and
        # normal marker quantisation can move a standing character one pixel
        # (about 0.007 here) without making them fall.  Using the exact
        # ±0.006 launch tolerance falsely started a layer-return climb after
        # those harmless hits, even though the current temporary anchor was
        # the strongest live evidence for its layer.
        anchor_match = bool(
            anchor is not None
            and abs(float(observation.player.y) - float(anchor.y))
            <= STATIONARY_ATTACK_SAME_LAYER_Y_TOLERANCE
        )
        if not after_confirmed_fall:
            return anchor_layer if anchor_match else self._detect_floor_all(observation)

        layers = {
            name: layer for name, layer in self.important_positions.items()
            if _has_layer_y_supporter(layer)
        }
        candidates = _layer_y_candidates(observation.player.y, layers)
        off_anchor = [name for name in candidates if name != anchor_layer]
        if off_anchor:
            floor = off_anchor[0]
            LOG.info(
                "STATIONARY RETURN: confirmed fall uses overlapping non-anchor "
                "layer %s instead of anchor %s (marker_y=%.6f)",
                floor, anchor_layer, observation.player.y,
            )
            return floor
        if anchor_match:
            return anchor_layer
        return self._detect_floor_all(observation)

    def _verify_out_of_range_floor(
        self,
        observation: MinimapObservation,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Periodically confirm a marker-only out-of-range landing.

        This verifier intentionally ignores world Y and vertical state. A
        monster can knock the character from an upper floor while the climb
        or planned-drop state still describes the old floor; those guards are
        useful during animation but must not suppress two stable readings on
        a recorded floor outside the patrol range.

        The planned descent to the route's first layer is the one exception, on
        purpose: it clears "stale vertical state" itself and would otherwise end the
        descent in the middle of a knock-down and start a return climb - the two
        recoveries fighting over the same character ("the back to base patrol layer
        should block the hit down by monster function, don't trigger back to patrol
        route").  It is bounded (``DROP_TO_FIRST_MAX_SECONDS``), so skipping the
        verifier for its duration cannot strand the character.
        """

        # A directional jump owns the full take-off/landing session.  An
        # airborne marker can temporarily resemble a lower floor; never let
        # the stake-return verifier cancel its Up hold in that interval.
        if self._jump_point_up_held:
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        checked_at = time.monotonic() if now is None else float(now)
        if self._descending_to_first:
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        if (checked_at - self._last_floor_verify_at
                < self._floor_verify_interval_seconds):
            return False
        self._last_floor_verify_at = checked_at
        if observation.player is None:
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        # A marker that is still inside the current patrol layer's recorded
        # band is not an out-of-range landing.  Adjacent layers can overlap
        # slightly by design, and an immediate return climb from that overlap
        # would make a character jump near a normal right-most endpoint.
        current_floor = self._current_route_floor()
        if (current_floor in self._route_layers
                and self._layer_band_contains(
                    current_floor, observation.player.y
                )):
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        marker_layers = {
            name: layer for name, layer in self.important_positions.items()
            if _has_layer_y_supporter(layer)
        }
        floor = detect_layer_by_y(observation.player.y, marker_layers)
        if floor is None or floor in self._route_layers:
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        if floor == self._floor_verify_candidate:
            self._floor_verify_frames += 1
        else:
            self._floor_verify_candidate = floor
            self._floor_verify_frames = 1
        if self._floor_verify_frames < 2:
            return False
        self._floor_verify_candidate = None
        self._floor_verify_frames = 0
        if self._return_mode is not None:
            return False

        LOG.warning(
            "POSITION VERIFIER: confirmed %s outside patrol range from "
            "two marker readings; clearing stale vertical state",
            floor,
        )
        self._descending_to_first = False
        self._release_climb_up()
        self._climb_state = ClimbState()
        self._fall_pending = False
        self._fall_floor_candidate = None
        self._fall_frames = 0
        self._fall_last_y = None
        self._fall_keys_released = False
        self._reset_fall_settle()
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.near_rope_event is not None:
            self.near_rope_event.clear()
        self._maybe_begin_return_if_out_of_range(observation)
        return self._return_mode is not None

    def _track_reconnect_window(self, now: float) -> None:
        """Arm one route check on the falling edge of 自动重连.

        While the reconnect runs, the automation gate stands this worker down, so no route decision
        is taken at all during the sequence (the login screens also hide the minimap, so there is
        nothing to measure).  The moment patrol input comes back, the character may have been put
        somewhere else - this makes the route check the first thing that happens.
        """

        event = self.reconnect_active_event
        if event is None:
            return
        if event.is_set():
            self._reconnect_was_active = True
            self._route_check_pending = False
            return
        if not self._reconnect_was_active:
            return
        self._reconnect_was_active = False
        self._route_check_pending = True
        self._route_check_deadline = float(now) + ROUTE_CHECK_AFTER_RECONNECT_SECONDS
        LOG.info("RECONNECT: input returned; checking the patrol route")

    def _note_reconnect_drop_attempt(
        self, observation: MinimapObservation, now: float,
    ) -> None:
        """Remember a real Alt+Down attempt for the reconnect-only fallback."""

        if (not self._reconnect_drop_recovery_armed
                or self._return_mode != "drop-to-route"
                or observation.player is None):
            return
        self._reconnect_drop_attempt_y = float(observation.player.y)
        self._reconnect_drop_attempt_at = float(now)

    def _reconnect_drop_recovery_decision(
        self, observation: MinimapObservation, now: float,
    ) -> Optional[MovementDecision]:
        """Return the post-reconnect edge-walk recovery action, when needed.

        This is intentionally not a general stuck/drop routine.  It is armed
        only by ``prepare_patrol_start(..., reconnect_restart=True)`` and only
        while the normal return mode is ``drop-to-route``.  A normal descent
        that gains Y never reaches either edge-walk phase.
        """

        player = observation.player
        if (not self._reconnect_drop_recovery_armed
                or self._return_mode != "drop-to-route"
                or player is None):
            return None
        current_y = float(player.y)

        phase = self._reconnect_drop_edge_phase
        if phase is not None:
            start_y = self._reconnect_drop_edge_start_y
            if (start_y is not None
                    and current_y >= start_y + RECONNECT_DROP_Y_PROGRESS):
                LOG.info(
                    "RECONNECT DROP RECOVERY: Y increased from %.6f to %.6f "
                    "while moving %s; returning to normal drop",
                    start_y, current_y, phase,
                )
                self._reconnect_drop_stalled_attempts = 0
                self._reconnect_drop_attempt_y = None
                self._reconnect_drop_edge_phase = None
                self._reconnect_drop_edge_start_y = None
                return None
            if now < self._reconnect_drop_edge_started_at + RECONNECT_DROP_EDGE_HOLD_SECONDS:
                remaining = (
                    self._reconnect_drop_edge_started_at
                    + RECONNECT_DROP_EDGE_HOLD_SECONDS - now
                )
                return MovementDecision(
                    phase,
                    f"reconnect drop recovery: holding {phase} to find a drop edge",
                    max(0.05, remaining),
                )
            if phase == "left":
                self._reconnect_drop_edge_phase = "right"
                self._reconnect_drop_edge_started_at = now
                self._reconnect_drop_edge_start_y = current_y
                LOG.warning(
                    "RECONNECT DROP RECOVERY: left edge found no descent; "
                    "holding right for %.1fs",
                    RECONNECT_DROP_EDGE_HOLD_SECONDS,
                )
                return MovementDecision(
                    "right",
                    "reconnect drop recovery: left edge did not descend; seeking right edge",
                    RECONNECT_DROP_EDGE_HOLD_SECONDS,
                )
            LOG.warning(
                "RECONNECT DROP RECOVERY: neither edge produced downward Y progress; "
                "resuming normal Alt+Down attempts",
            )
            self._reconnect_drop_stalled_attempts = 0
            self._reconnect_drop_attempt_y = None
            self._reconnect_drop_edge_phase = None
            self._reconnect_drop_edge_start_y = None
            return None

        attempt_y = self._reconnect_drop_attempt_y
        attempt_at = self._reconnect_drop_attempt_at
        if (attempt_y is None
                or self._reconnect_drop_assessed_at == attempt_at
                or now - attempt_at < self.drop_retry_seconds):
            return None
        # Each physical drop chord is assessed exactly once, immediately
        # before the next one would be allowed.  Higher marker Y means the
        # character genuinely descended; otherwise it is one stalled attempt.
        self._reconnect_drop_assessed_at = attempt_at
        if current_y >= attempt_y + RECONNECT_DROP_Y_PROGRESS:
            self._reconnect_drop_stalled_attempts = 0
            self._reconnect_drop_attempt_y = None
            return None
        self._reconnect_drop_stalled_attempts += 1
        self._reconnect_drop_attempt_y = None
        LOG.info(
            "RECONNECT DROP RECOVERY: Alt+Down attempt %d/%d made no downward Y progress "
            "(%.6f -> %.6f)",
            self._reconnect_drop_stalled_attempts,
            RECONNECT_DROP_STALLED_ATTEMPTS,
            attempt_y, current_y,
        )
        if self._reconnect_drop_stalled_attempts < RECONNECT_DROP_STALLED_ATTEMPTS:
            return None
        self._reconnect_drop_edge_phase = "left"
        self._reconnect_drop_edge_started_at = now
        self._reconnect_drop_edge_start_y = current_y
        LOG.warning(
            "RECONNECT DROP RECOVERY: %d Alt+Down attempts stalled; "
            "holding left for %.1fs",
            RECONNECT_DROP_STALLED_ATTEMPTS,
            RECONNECT_DROP_EDGE_HOLD_SECONDS,
        )
        return MovementDecision(
            "left",
            "reconnect drop recovery: Alt+Down stalled; seeking left drop edge",
            RECONNECT_DROP_EDGE_HOLD_SECONDS,
        )

    def _reset_drop_arrival(self) -> None:
        """Forget the drop-to-route landing evidence (movement thread only)."""

        self._drop_entry_y = None
        self._drop_lowest_y = None
        self._drop_last_y = None
        self._drop_settled = False
        self._drop_arrival_candidate = None
        self._drop_arrival_frames = 0
        self._drop_relaxed_y = None
        self._drop_relaxed_floor = None

    def _note_drop_descent(self, observation: MinimapObservation) -> None:
        """Remember where the current drop-to-route descent started and got to.

        The entry Y is taken from the first frame of the descent and is NOT
        reset by a transient missing marker (the caller only resets when the
        return mode itself has ended), so one lost reading cannot restart the
        measured descent.
        """

        player = observation.player
        if player is None:
            # No fresh reading: keep the evidence and the settle flag as they
            # are rather than claiming the marker settled.
            self._drop_settled = False
            return
        y = float(player.y)
        if self._drop_entry_y is None:
            self._drop_entry_y = y
        if self._drop_lowest_y is None or y > self._drop_lowest_y:
            self._drop_lowest_y = y
        # A falling marker sweeps down the minimap by ``fall_marker_y_gain``
        # per frame; a character standing on a landing moves less than that.
        self._drop_settled = bool(
            self._drop_last_y is None
            or abs(y - self._drop_last_y) < self._fall_marker_y_gain
        )
        self._drop_last_y = y

    def _drop_descended(self) -> bool:
        """True when the drop has really moved the marker down from its start.

        A descent may never be finished from a reading that only repeats where
        it began, and never before one Alt+Down chord was actually sent: the
        world-Y origin is deliberately anchored to the route's TOP floor while
        the character is still above it, so the very first reading of a fresh
        "start above the route" must not be allowed to declare an arrival.
        """

        if self._last_drop_attempt == float("-inf"):
            return False
        entry = self._drop_entry_y
        lowest = self._drop_lowest_y
        if entry is None or lowest is None:
            return False
        return bool(lowest - entry >= RECONNECT_DROP_Y_PROGRESS)

    def _drop_landing_floor(
        self, observation: MinimapObservation
    ) -> Optional[str]:
        """The floor the drop-to-route descent has landed on, or ``None``.

        The marker's own recorded band is direct evidence and is still accepted
        immediately.  A landing away from the recorded row matches no band at
        all, and that is where the character used to stay in the drop phase
        forever, sending one Alt+Down chord after another while already standing
        on a patrol floor.  The same marker-only answers ordinary patrol uses
        for that reading resolve it here:

        1. at/below the bottom recorded floor - nothing is recorded lower, so
           the descent cannot continue; the floor is handed to ``_finish_return``
           (patrol it, or climb back when the patrol range starts above it);
        2. the bounded nearest recorded patrol floor ("if the character can't
           find a layer he should anchor to the nearest layer").

        Both need real drop evidence first, and the answer must hold while the
        marker is settled: reading a floor that the character is merely falling
        THROUGH would restart patrol in mid-air.  The world-Y tracker is
        deliberately never consulted here (see the caller).
        """

        player = observation.player
        if player is None:
            return None
        marker_y = float(player.y)
        route_layers = {
            name: self.important_positions[name]
            for name in self._route_layers
            if name in self.important_positions
        }
        # 1) The marker's own recorded band is direct evidence of the visible
        #    floor and is still accepted at once, exactly as before.
        floor = detect_layer_by_y(marker_y, route_layers)
        if floor is not None:
            self._drop_arrival_candidate = None
            self._drop_arrival_frames = 0
            return floor
        # 2) The relaxed answers below need real drop evidence: an Alt+Down
        #    chord was sent, the marker really moved down from where the descent
        #    started, and the reading is no longer sweeping (a character falling
        #    THROUGH the floor moves by ``fall_marker_y_gain`` per frame and must
        #    never be read as a landing).  With no recorded patrol floor there is
        #    nothing to anchor to, so the drop keeps its previous behaviour.
        if (not route_layers
                or not self._drop_descended()
                or not self._drop_settled):
            self._drop_arrival_candidate = None
            self._drop_arrival_frames = 0
            self._drop_relaxed_y = None
            self._drop_relaxed_floor = None
            return None
        if (self._drop_relaxed_y is None
                or abs(marker_y - self._drop_relaxed_y) > 1e-9):
            # Resolve once per reading: the shared fallbacks log their evidence
            # when they answer, and a standing character repeats the same Y.
            landing = self._bottom_floor_for_marker_y(marker_y)
            if landing is None or landing not in self.important_positions:
                landing = self._nearest_floor_by_marker_y(
                    marker_y,
                    route_layers,
                    max_distance=LAYER_NEAREST_FLOOR_MAX_DISTANCE,
                )
            self._drop_relaxed_y = marker_y
            self._drop_relaxed_floor = landing
        landing = self._drop_relaxed_floor
        if landing is None:
            self._drop_arrival_candidate = None
            self._drop_arrival_frames = 0
            return None
        if landing == self._drop_arrival_candidate:
            self._drop_arrival_frames += 1
        else:
            self._drop_arrival_candidate = landing
            self._drop_arrival_frames = 1
        if self._drop_arrival_frames < DROP_ARRIVAL_CONFIRM_FRAMES:
            return None
        self._drop_arrival_candidate = None
        self._drop_arrival_frames = 0
        entry_y = self._drop_entry_y
        lowest_y = self._drop_lowest_y
        moved = (
            float(lowest_y) - float(entry_y)
            if entry_y is not None and lowest_y is not None else 0.0
        )
        LOG.warning(
            "DROP TO ROUTE: accepting %s as the landing floor (marker y=%.6f "
            "matches no patrol-floor band; the drop moved the marker %.6f down "
            "and the reading held for %d frames); restarting patrol",
            landing,
            marker_y,
            moved,
            DROP_ARRIVAL_CONFIRM_FRAMES,
        )
        return landing

    def _run_pending_route_check(
        self, observation: MinimapObservation, now: float
    ) -> bool:
        """One bounded post-reconnect route check; True once it is decided.

        Returns False while it is still waiting for a usable reading - the caller runs every frame,
        and the deadline hands the decision back to the normal verifier so a character standing on
        an unrecorded platform cannot keep this waiting forever.
        """

        if not self._route_check_pending:
            return False
        if (self.stationary_attack_enabled
                or not self.patrol_enabled
                or not self._route_layers):
            # 站桩攻击 has no route to return to, and with no recorded range there is nothing to
            # compare against.  Both cases simply leave the previous behaviour in place.
            self._route_check_pending = False
            return True
        if (self._return_mode is not None
                or self._climb_state.up_held
                or self._climb_state.phase != "idle"):
            # A return/climb already owns the vertical state and re-checks the floor itself.
            self._route_check_pending = False
            return True
        if observation.player is None:
            if now >= self._route_check_deadline:
                self._route_check_pending = False
                LOG.warning(
                    "RECONNECT: no character marker for %.1fs after the reconnect; "
                    "the normal floor verifier keeps watching",
                    ROUTE_CHECK_AFTER_RECONNECT_SECONDS,
                )
            return False
        floor = self._detect_floor_all(observation)
        if floor is None:
            if now >= self._route_check_deadline:
                self._route_check_pending = False
                LOG.warning(
                    "RECONNECT: the marker matches no recorded floor; "
                    "keeping the current route state"
                )
            return False
        # The world-Y origin was anchored before the disconnect; the login screens hid the minimap
        # for the whole sequence, so re-anchor it to the floor the marker shows now BEFORE any
        # layer decision is taken from world Y.
        self._reanchor_tracker_to_layer(floor, observation)
        if floor in self._route_layers:
            self._route_check_pending = False
            LOG.info(
                "RECONNECT: on %s inside the patrol route; patrol continues", floor
            )
            return True
        self._maybe_begin_return_if_out_of_range(observation)
        if self._return_mode is None:
            if now >= self._route_check_deadline:
                self._route_check_pending = False
                LOG.warning(
                    "RECONNECT: %s is outside the patrol route but the return "
                    "could not start; leaving it to the normal verifier",
                    floor,
                )
            return False
        self._route_check_pending = False
        LOG.warning(
            "RECONNECT: on %s outside the patrol route; %s",
            floor,
            "climbing back" if self._return_mode == "climb-to-route" else "dropping back",
        )
        return True

    def _finish_return(
        self,
        floor: str,
        observation: Optional[MinimapObservation] = None,
    ) -> None:
        """After a return climb/drop reaches ``floor``: in range or not?  An
        in-range floor restarts patrol there; an out-of-range floor keeps the
        return mode pointed at the next step."""
        self._return_from_floor = floor
        stationary_anchor_layer = self._stationary_route_anchor_layer
        if (self.stationary_attack_enabled
                and self._stationary_return_route_ready
                and stationary_anchor_layer):
            if floor == stationary_anchor_layer:
                was_climbing = bool(
                    self._return_mode == "climb-to-route"
                    and (self._climb_state.up_held
                         or self._climb_state.phase != "idle")
                )
                if was_climbing:
                    self._climb_arrival_at = time.monotonic()
                anchor = self._stationary_attack_anchor
                player = observation.player if observation is not None else None
                # The layer confirmation deliberately completes while still
                # holding Up.  On a rope-top frame that is correct for the
                # vertical transition but not yet a usable standing floor:
                # horizontal 0.12s recovery taps are swallowed by the rope.
                # Schedule one full lateral dismount toward the temporary
                # anchor before allowing any pickup retry to start.
                if (was_climbing and anchor is not None and player is not None
                        and abs(anchor.x - player.x) > 0.001):
                    self._stationary_return_dismount_direction = (
                        "right" if anchor.x > player.x else "left"
                    )
                else:
                    self._stationary_return_dismount_direction = None
                self._release_climb_up()
                self._climb_state = ClimbState()
                self._return_mode = None
                self._return_arrival_floor = None
                self._patrol_busy_until = 0.0
                self._stationary_pickup_return_active = False
                if floor in self._route_layers:
                    self._route_layer_index = self._route_layers.index(floor)
                self._route_phase = "left"
                self._reanchor_tracker_to_layer(floor, observation)
                if self.stationary_pickup_enabled:
                    # A confirmed fall discarded the old pickup plan.  Only
                    # after the normal route return reaches this layer do we
                    # begin a completely new interval.
                    self._stationary_pickup_next_at = (
                        time.monotonic()
                        + self._stationary_pickup_delay()
                    )
                    LOG.info(
                        "STATIONARY PICKUP: route return reached 桩; "
                        "fresh pickup interval armed"
                    )
                LOG.warning(
                    "STATIONARY RETURN: reached anchor layer %s; resuming stand-still attack",
                    floor,
                )
                return
            # Reaching an intermediate recorded layer is progress, not the
            # destination. Keep the existing return-climb machinery pointed
            # at this layer's rope until the temporary anchor layer is met.
            self._return_mode = (
                "climb-to-route"
                if _layer_number(floor) < _layer_number(stationary_anchor_layer)
                else "drop-to-route"
            )
            self._return_from_floor = floor
            self._return_arrival_floor = None
            if floor in self._route_layers:
                # The common layer detector uses this index to decide whether
                # an upper-layer reading is a genuine climb arrival.  Keep it
                # at the confirmed intermediate floor, not at the final
                # stationary anchor, or every layer2 sample is treated as an
                # already-current layer and its confirmation counter resets.
                self._route_layer_index = self._route_layers.index(floor)
            self._climb_state.target_layer_frames = 0
            self._climb_state.target_layer_since = None
            LOG.info(
                "STATIONARY RETURN: reached intermediate %s; continuing toward %s",
                floor, stationary_anchor_layer,
            )
            return
        if floor in self._route_layers:
            # A confirmed return completes the reconnect-only edge recovery,
            # so it can never leak into a later manual Start Patrol.
            self._reconnect_drop_recovery_armed = False
            self._reconnect_drop_attempt_y = None
            self._reconnect_drop_attempt_at = float("-inf")
            self._reconnect_drop_assessed_at = float("-inf")
            self._reconnect_drop_stalled_attempts = 0
            self._reconnect_drop_edge_phase = None
            self._reconnect_drop_edge_start_y = None
            # A return climb can reach the route while persistent Up is still
            # owned.  Release it before resetting the climb state; otherwise
            # ``_start_patrol_on`` forgets the ownership flag and the physical
            # Up key can remain held into the resumed layer2 patrol.
            was_climbing = bool(
                self._return_mode == "climb-to-route"
                and (self._climb_state.up_held
                     or self._climb_state.phase != "idle")
            )
            if was_climbing:
                self._climb_arrival_at = time.monotonic()
            self._release_climb_up()
            self._patrol_busy_until = 0.0
            self._return_mode = None
            self._return_arrival_floor = None
            # Keep the confirmed landing position: stair/bench layers can
            # have a different recorded world-Y at each action point.
            self._start_patrol_on(floor, observation)
            LOG.warning("RETURN TO ROUTE: reached %s; restarting patrol", floor)
        else:
            number = _layer_number(floor)
            self._return_mode = (
                "climb-to-route" if number < self._patrol_range_min
                else "drop-to-route"
            )
            self._return_from_floor = floor
            self._return_arrival_floor = None
            self._climb_state.target_layer_frames = 0
            self._climb_state.target_layer_since = None
            LOG.warning(
                "RETURN TO ROUTE: still on %s outside range; %s",
                floor,
                "climbing back" if self._return_mode == "climb-to-route"
                else "dropping back",
            )

    def _return_climb_arrival_ready(self, floor: Optional[str]) -> bool:
        """Confirm and settle a return climb before resuming patrol."""

        state = self._climb_state
        stationary_anchor_layer = (
            self._stationary_route_anchor_layer
            if (self.stationary_attack_enabled
                and self._stationary_return_route_ready) else None
        )
        # A stand-still return deliberately narrows the active patrol range
        # to the temporary stake layer.  The floors below it are consequently
        # *not* in ``_route_layers`` even though their recorded ropes are the
        # route home.  Treat a higher recorded floor between the current
        # departure floor and the stake as a real climb arrival: _finish_return
        # will then make that floor the next rope source.  Without this, a
        # physical arrival on layer2 was rejected as off-route and the old
        # layer1 climb state kept sending Alt jumps in place.
        def stationary_intermediate(candidate: Optional[str]) -> bool:
            return bool(
                stationary_anchor_layer is not None
                and candidate is not None
                and candidate in self.important_positions
                and self._return_from_floor is not None
                and _layer_number(self._return_from_floor)
                < _layer_number(candidate)
                < _layer_number(stationary_anchor_layer)
            )

        def route_intermediate(candidate: Optional[str]) -> bool:
            """A recorded floor the return climb has just climbed UP to.

            The return target is the bottom of the patrol range, so the floor
            between the departed one and that target is *outside*
            ``_route_layers`` while its rope is the way home - the operator's
            layer2 with the range starting at layer3.  Rejecting it here left the
            climb stuck at the platform top: the stall detector released Up and
            re-jumped forever (13:58 log: "CLIMB stalled: world Y stopped
            advancing" while standing on layer2's own row at world 3.546).
            Accepting it hands the floor to ``_finish_return``, which already
            knows how to rebase an out-of-range floor and continue the return
            from that floor's own rope.
            """

            return bool(
                candidate is not None
                and candidate in self.important_positions
                and candidate not in self._route_layers
                and self._return_mode == "climb-to-route"
                and self._return_from_floor is not None
                and _layer_number(self._return_from_floor)
                < _layer_number(candidate)
                <= self._patrol_range_min
            )

        def accepted(candidate: Optional[str]) -> bool:
            return bool(
                candidate is not None
                and (candidate in self._route_layers
                     or candidate == stationary_anchor_layer
                     or stationary_intermediate(candidate)
                     or route_intermediate(candidate))
            )

        if floor is None:
            # A blank frame during a climb is the character's own jump arc just
            # above the platform row, not evidence against the arrival.  Zeroing
            # the streak here made arrival impossible to confirm; a long blank
            # run still resets it.
            state.blank_arrival_frames += 1
            if state.blank_arrival_frames < CLIMB_ARRIVAL_BLANK_FRAMES_TOLERATED:
                return False
            self._return_arrival_floor = None
            state.target_layer_frames = 0
            state.target_layer_since = None
            return False
        state.blank_arrival_frames = 0

        arrival_is_route_or_stationary_anchor = accepted(floor)
        if (state.target_layer_since is not None
                and accepted(self._return_arrival_floor)):
            elapsed = time.monotonic() - state.target_layer_since
            if elapsed < self.climb_layer_confirm_seconds:
                LOG.info(
                    "RETURN CLIMB top compensation: %s %.2f/%.2fs; "
                    "keeping Up held",
                    self._return_arrival_floor,
                    elapsed,
                    self.climb_layer_confirm_seconds,
                )
                return False
            return True

        if not arrival_is_route_or_stationary_anchor:
            self._return_arrival_floor = None
            state.target_layer_frames = 0
            state.target_layer_since = None
            return False
        if floor != self._return_arrival_floor:
            self._return_arrival_floor = floor
            state.target_layer_frames = 0
            state.target_layer_since = None
        state.target_layer_frames += 1
        if state.target_layer_frames < self.climb_layer_confirm_frames:
            LOG.info(
                "RETURN CLIMB arrival confirmation: %s %d/%d; keeping Up held",
                floor,
                state.target_layer_frames,
                self.climb_layer_confirm_frames,
            )
            return False
        if self.climb_layer_confirm_seconds > 0:
            state.target_layer_since = time.monotonic()
            LOG.info(
                "RETURN CLIMB layer %s confirmed; compensating Up for %.2fs",
                floor,
                self.climb_layer_confirm_seconds,
            )
            return False
        return True

    def _restart_return_climb_after_descent(
        self,
        floor: Optional[str],
        observation: MinimapObservation,
    ) -> bool:
        """Rebase a failed return climb on the lower floor actually reached.

        A return climb is normally allowed to change floors only after it has
        climbed *up* into its expected destination.  That rule prevented a
        noisy early reading from swapping rope targets, but also meant that a
        missed rope could leave the state permanently targeting the departed
        floor.  The character had already fallen to layer1 while the worker
        repeatedly tried layer2's rope.

        Two consecutive lower-floor reads are enough to prove a real descent.
        We then release the old Up hold and start the normal return logic from
        the landing floor.  This is deliberately a return-climb repair only;
        it does not involve the stationary recovery-jump detector.
        """

        departing_floor = self._return_from_floor
        state = self._climb_state
        if (
            floor is None
            or departing_floor is None
            or floor == departing_floor
            or _layer_number(floor) >= _layer_number(departing_floor)
        ):
            state.return_descent_floor = None
            state.return_descent_frames = 0
            return False

        if floor != state.return_descent_floor:
            state.return_descent_floor = floor
            state.return_descent_frames = 0
        state.return_descent_frames += 1
        if state.return_descent_frames < 2:
            LOG.info(
                "RETURN CLIMB: lower landing candidate %s below %s %d/2",
                floor,
                departing_floor,
                state.return_descent_frames,
            )
            return False

        anchor_layer = self._stationary_route_anchor_layer
        self._release_climb_up()
        self._climb_state = ClimbState()
        self._return_from_floor = floor
        self._return_arrival_floor = None
        self._reanchor_tracker_to_layer(floor, observation)
        if floor in self._route_layers:
            self._route_layer_index = self._route_layers.index(floor)
        LOG.warning(
            "RETURN CLIMB: missed the rope from %s and landed on %s; "
            "restarting the route return%s",
            departing_floor,
            floor,
            " toward %s" % anchor_layer if anchor_layer else "",
        )
        return True

    def _reset_fall_settle(self) -> None:
        """Clear the landing-reconciliation settle state."""

        self._fall_settle_started = None
        self._fall_settle_world = []

    def _reset_fall_tracking(self) -> None:
        """Forget every fall-detection state, so a new vertical phase starts clean.

        ``_track_fall`` is suppressed while the planned descent to the first floor runs, and the
        suppression clears only the frame counters - a ``_fall_pending`` flag and its settle window from
        before the descent survive it.  When the descent then handed the loop back to patrol, the flag
        was still set and the settle window had been "stable" for seconds (the tracker had not moved at
        all), so ``_reconcile_landed_floor`` answered with the world-Y captured BEFORE the descent and the
        worker printed "FALL RECOVERY: landed on <the floor it came from>; restarting patrol" - the
        operator's report: the character dropped back to the route's first layer and the patrol restarted
        on layer2 instead.

        Called when the planned descent starts and when the loop restarts on the first layer (see
        ``_reset_route_loop``), so no stale fall can ever resolve across those two boundaries.
        """

        self._fall_pending = False
        self._fall_floor_candidate = None
        self._fall_frames = 0
        self._fall_last_y = None
        self._fall_keys_released = False
        self._reset_fall_settle()

    def _reconcile_landed_floor(
        self, observation: MinimapObservation
    ) -> Optional[str]:
        """Resolve the landing floor after a fall - world-Y authoritative.

        The raw marker Y is screen-relative on a scrolling minimap and the
        OpenCV world-Y tracker LAGS a fast knock-down fall, so the landing
        floor is resolved from the raw (pre-pin) world-Y reading only after
        it has stabilized (or the settle window times out).  World-Y bands
        over every recorded layer are the primary evidence; the marker Y is
        only a fallback when it matches exactly one recorded layer; and a
        reading at/below the lowest recorded band resolves to the bottom
        floor (nothing lower exists).  Returns None while still settling or
        genuinely unknown - the caller keeps the fall pending and retries on
        the next frame.
        """

        now = time.monotonic()
        if self._fall_settle_started is None:
            self._fall_settle_started = now
        world_ok = bool(
            self._raw_world_y is not None
            and self._raw_structure_confidence >= 0.12
        )
        if world_ok:
            self._fall_settle_world.append(self._raw_world_y)
            if len(self._fall_settle_world) > 8:
                self._fall_settle_world.pop(0)
        settled = False
        if len(self._fall_settle_world) >= self.fall_settle_min_frames:
            window = self._fall_settle_world[-self.fall_settle_min_frames:]
            settled = (
                max(window) - min(window)
            ) <= self.fall_settle_epsilon
        timed_out = (
            now - self._fall_settle_started
        ) >= self.fall_settle_max_seconds
        floor: Optional[str] = None
        if settled or (timed_out and world_ok):
            world_layers = {
                name: layer
                for name, layer in self.important_positions.items()
                if isinstance(layer, dict) and "layer_world_y" in layer
            }
            if world_layers:
                floor = detect_layer_by_world_y(
                    self._fall_settle_world[-1], world_layers
                )
                if floor is not None:
                    LOG.info(
                        "FALL RECONCILE: settled worldY %.6f -> %s",
                        self._fall_settle_world[-1], floor,
                    )
        if floor is None and observation.player is not None:
            # Marker-Y fallback: only an unambiguous single-band reading is
            # accepted - screen Y is scroll-dependent, so ambiguous readings
            # are ignored rather than guessed.
            marker_layers = {
                name: layer
                for name, layer in self.important_positions.items()
                if _has_layer_y_supporter(layer)
            }
            candidates = _layer_y_candidates(
                observation.player.y, marker_layers
            )
            if len(candidates) == 1:
                floor = candidates[0]
                LOG.info(
                    "FALL RECONCILE: marker fallback y=%.6f -> %s",
                    observation.player.y, floor,
                )
        if floor is None and observation.player is not None:
            # At/below the lowest recorded band: the character is on (or
            # under) the bottom floor - nothing lower exists.  This is what
            # guarantees layer1 is recognized after knock-downs whose exact
            # landing spot the recorded band does not cover.
            bottom_floor = self._bottom_recorded_layer()
            bottom_layer = (
                self.important_positions.get(bottom_floor, {})
                if bottom_floor is not None else {}
            )
            band = (
                _layer_y_band(
                    bottom_layer,
                    float(bottom_layer.get("y_tolerance", 0.020000)),
                )
                if isinstance(bottom_layer, dict) else None
            )
            if band is not None and observation.player.y >= band[1] - 1e-9:
                floor = bottom_floor
                LOG.warning(
                    "FALL RECONCILE: marker y=%.6f at/below the %s band "
                    "(bottom %.6f); resolving to the bottom floor",
                    observation.player.y, bottom_floor, band[1],
                )
        if floor is not None:
            # Physical limit before acting on it: the landing may not be a floor the marker draws the
            # character below.  The world-Y tracker lags a fast knock-down and then sits still at the
            # floor the character fell FROM, and "still" is exactly what the settle test above accepts.
            floor = self._cap_landing_floor(
                floor, observation, source="FALL RECONCILE"
            )
        if floor is None and not timed_out:
            # Still absorbing the tracker lag: keep the fall pending.
            return None
        return floor

    def _resolve_fall(self, observation: MinimapObservation) -> bool:
        """Called when a fall stops: resolve the true landing floor and act.

        The landing floor is resolved through ``_reconcile_landed_floor``
        (world-Y authoritative after a settle window - see there) and the
        tracker is re-anchored to it, so a monster knock-down can never
        leave the world-Y origin on the pre-fall floor.  An in-range floor
        restarts patrol there (a same-floor bounce is ignored and patrol
        continues); an out-of-range floor starts the return-to-route
        climb/drop.  Returns False while the landing is still being
        resolved (kept pending for the next frame).
        """
        floor = self._reconcile_landed_floor(observation)
        if floor is None:
            return False
        self._fall_pending = False
        self._fall_floor_candidate = None
        self._fall_last_y = None
        self._fall_frames = 0
        self._fall_keys_released = False
        self._reset_fall_settle()
        self._last_fall_resolved_at = time.monotonic()
        if self._return_mode is not None:
            if (self._return_mode == "climb-to-route"
                    and floor in self._route_layers
                    and not self._return_climb_arrival_ready(floor)):
                # Do not re-anchor from the first apparent arrival frame. A
                # bench jump can briefly enter an upper marker band while it
                # is still part of the current logical layer.
                return False
            confirmed_floor = self._return_arrival_floor or floor
            # Re-anchor only after the landing floor has been confirmed. X
            # selects/interpolates the recorded per-point world-Y on a stair
            # or bench layer.
            if confirmed_floor not in self._route_layers:
                self._reanchor_tracker_to_layer(confirmed_floor, observation)
            self._finish_return(confirmed_floor, observation)
            return True
        if floor in self._route_layers:
            if self._current_route_floor() == floor:
                # A stair/bench jump is a same-layer bounce, not a floor
                # transition. Re-anchoring here used to turn the bench into a
                # new world origin and made the following rope arrival miss.
                return True
            self._start_patrol_on(floor, observation)
            LOG.warning("FALL RECOVERY: landed on %s; restarting patrol", floor)
        else:
            number = _layer_number(floor)
            self._reanchor_tracker_to_layer(floor, observation)
            self._return_mode = (
                "climb-to-route" if number < self._patrol_range_min
                else "drop-to-route"
            )
            self._return_from_floor = floor
            self._return_arrival_floor = None
            LOG.warning(
                "FALL RECOVERY: landed on %s outside patrol range; %s",
                floor,
                "climbing back to route"
                if self._return_mode == "climb-to-route"
                else "dropping back to route",
            )
        return True

    def _track_fall(self, observation: MinimapObservation) -> None:
        """Per-frame falling detector (see the FALLING RECOVERY state docs).

        Suppressed while the intentional drop-to-layer1 descent, a return
        drop, or an active rope climb is running - those are never
        interrupted.  A fall is N consecutive frames of the diamond Y
        dropping fast; once it stops the floor is re-detected and
        ``_resolve_fall`` restarts patrol or starts the return.
        """
        if self._jump_point_up_held:
            # Keep the fall samples intact. Once the jump landing routine
            # releases Up after stable Y, the next frame can distinguish a
            # genuine lower-floor landing from a successful rope/platform
            # arrival and start a return only if it is still needed.
            return
        if (self._descending_to_first
                or self._return_mode is not None
                or self._climb_state.phase != "idle"
                or self._climb_state.up_held):
            self._fall_frames = 0
            self._fall_last_y = None
            return
        if observation.player is None:
            self._fall_frames = 0
            self._fall_last_y = None
            return
        y = observation.player.y
        last = self._fall_last_y
        self._fall_last_y = y
        if last is None:
            self._fall_frames = 0
            return
        if y - last >= self._fall_marker_y_gain:
            if self._fall_frames == 0 and not self._fall_pending:
                # New descent: evidence from an earlier landing must never
                # decide this one.
                self._fall_floor_candidate = None
            self._fall_frames += 1
            # A knock-down mid-action can leave a key stuck in the game (its
            # key-up was lost during the knock): release EVERY movement key
            # once as soon as the fall is confirmed, so the character lands
            # free instead of being pushed against the first wall in the
            # stuck direction (observed: stuck Left pinned at the left edges
            # of layer2 and layer1 after a knock-down).
            if (self._fall_frames >= self._fall_detect_frames
                    and not self._fall_keys_released):
                self._fall_keys_released = True
                self._release_stuck_keys()
            return
        # The fall stopped (marker Y no longer dropping fast).  A stationary
        # return must only be forced here after an actual confirmed fall.
        # This method also runs on every quiet frame; treating every quiet
        # frame as a fall made an overlapping anchor band capable of hiding a
        # real lower landing.
        confirmed_fall = bool(
            self._fall_frames >= self._fall_detect_frames
            or self._fall_pending
            or self._fall_keys_released
        )
        if (confirmed_fall
                and self.stationary_attack_enabled
                and self._stationary_return_route_ready):
            # The falling detector is shared with patrol mode.  Once the
            # marker settles away from the temporary anchor's recorded layer,
            # discard any pickup plan immediately, then hand vertical work to
            # the ordinary rope return state.  This happens even while a
            # jump-point Up hold delays route selection by a frame or two.
            self._stationary_x_settled = False
            self._abort_stationary_pickup_for_route_return()
            self._begin_stationary_return(
                observation, after_confirmed_fall=True
            )
            self._fall_pending = False
            self._fall_frames = 0
            self._fall_last_y = None
            self._fall_keys_released = False
            self._reset_fall_settle()
            return
        if self.stationary_attack_enabled:
            # Plain 站桩攻击 has no route floor to resolve and no return to start:
            # the temporary anchor owns displacement.  Keep only the stuck-key
            # release above, so a knock-down can never latch a route state
            # that blocks the attacks.
            self._fall_pending = False
            self._fall_frames = 0
            self._fall_last_y = None
            self._fall_keys_released = False
            self._reset_fall_settle()
            return
        if self._fall_pending:
            self._resolve_fall(observation)
        elif self._fall_frames >= self._fall_detect_frames:
            self._fall_pending = True
            self._resolve_fall(observation)
        self._fall_frames = 0

    def _world_drift_check(
        self, observation: MinimapObservation, now: float
    ) -> None:
        """Re-anchor the world-Y tracker when it drifts while cruising.

        The tracker prefers incremental phase correlation, whose per-frame
        error accumulates: over minutes the world-Y origin slowly shifts
        even while the character stands or walks on the same floor, and a
        shifted origin makes layer recognition wrong.  Every confirmed
        landing/arrival re-anchors, but between those events this throttled
        check compares the RAW world Y against the believed floor's expected
        anchor at the marker X and re-anchors silently when the gap exceeds
        ``world_drift_reanchor_threshold``.  It only runs while the floor
        belief is corroborated by an in-band marker reading and no vertical
        action is active, so it can never fight a climb/drop/return.
        """

        if (now - self._last_world_drift_check
                < self.world_drift_check_interval_seconds):
            return
        self._last_world_drift_check = now
        if (self._climb_state.phase != "idle"
                or self._climb_state.up_held
                or self._return_mode is not None
                or self._descending_to_first
                or self._fall_pending
                or observation.player is None):
            return
        if (self._last_fall_resolved_at is not None
                and now - self._last_fall_resolved_at < 1.5):
            # A fresh landing reconcile owns the origin; do not race it.
            return
        if (self._raw_world_y is None
                or self._raw_structure_confidence < 0.12):
            return
        if not self.patrol_enabled or not self._route_layers:
            return
        floor = self._current_route_floor()
        if floor is None:
            return
        if not self._layer_band_contains(floor, observation.player.y):
            return
        layer = self.important_positions.get(floor, {})
        expected = _layer_world_anchor_at_x(layer, observation.player.x)
        if expected is None:
            return
        gap = abs(self._raw_world_y - expected)
        if gap >= self.world_drift_reanchor_threshold:
            LOG.warning(
                "WORLD-Y DRIFT: raw worldY %.6f is %.3f away from the %s "
                "anchor at x=%.4f; re-anchoring",
                self._raw_world_y, gap, floor, observation.player.x,
            )
            self._reanchor_tracker_to_current_layer(observation)

    def _note_fall_floor_candidate(self, floor: str) -> None:
        """Keep the lowest trustworthy outside-route floor seen in a fall."""

        if floor not in self.important_positions:
            return
        current = self._fall_floor_candidate
        if (current is not None
                and _layer_number(floor) >= _layer_number(current)):
            return
        self._fall_floor_candidate = floor
        LOG.info(
            "FALL CANDIDATE: observed %s outside patrol route; holding it "
            "until descent settles",
            floor,
        )

    def _maybe_begin_return_if_out_of_range(
        self, observation: MinimapObservation, *,
        confirmed_floor: Optional[str] = None,
    ) -> None:
        """Begin return after a caller has confirmed an out-of-range floor.

        Patrol startup and settled falls have their own direct recovery
        paths. During normal patrol this helper is called only by the
        two-sample marker verifier; a single ambiguous marker/world-Y reading
        must never start a rope climb near a route endpoint.

        The planned descent to the route's first layer BLOCKS this on purpose
        (the operator: "the back to base patrol layer should block the hit down by
        monster function, don't trigger back to patrol route"): while the loop is
        walking itself back down to its first floor, a knock-down reading must not
        start a return climb, or the two recoveries fight over the same character.
        The descent ends at its own arrival test (``_final_drop_arrived``) or at
        ``DROP_TO_FIRST_MAX_SECONDS``, and only then can this run.
        """
        if (self._return_mode is not None
                or self._descending_to_first
                or self._climb_state.phase != "idle"
                or self._climb_state.up_held
                or (self.stationary_attack_enabled
                    and not self._stationary_return_route_ready)
                or observation.player is None):
            return
        floor = confirmed_floor or self._detect_floor_all(observation)
        if floor is None or floor in self._route_layers:
            return
        if floor not in self.important_positions:
            return
        number = _layer_number(floor)
        self._reanchor_tracker_to_layer(floor, observation)
        self._return_mode = (
            "climb-to-route" if number < self._patrol_range_min
            else "drop-to-route"
        )
        self._return_from_floor = floor
        self._return_arrival_floor = None
        self._fall_floor_candidate = None
        LOG.warning(
            "OUT OF PATROL RANGE: on %s outside patrol range; returning %s "
            "without attacking",
            floor,
            "climbing back" if self._return_mode == "climb-to-route"
            else "dropping back",
        )

    def _rope_target_for_floor(
        self, floor: str
    ) -> tuple[Optional[float], str]:
        """Approach X for ``floor``'s rope, and the reason for that answer.

        A recorded ``rope_pos`` is exact.  The legacy profile-wide ``rope.x``
        (``fixed_target_x``) is NOT a substitute: it belongs to whichever floor
        first recorded a rope.  Using it for the operator's layer2 - which has no
        rope recorded, only a 右跳 at x=0.469298 - walked the character left to
        x=0.5 and off the platform, dropping it to layer1 (13:16 log).

        When the floor has no rope but does have recorded jump points, the
        nearest one to that legacy rope X is the operator's own way onto the
        rope: it is used as the approach target, so the walk crosses it in its
        recorded direction and the point fires the mount jump.

        The answer is logged once per floor and answer (this runs every frame).
        """

        layer = self.important_positions.get(floor, {})
        if not isinstance(layer, dict):
            answer: tuple[Optional[float], str] = (
                None, "is not a recorded floor",
            )
        else:
            rope = layer.get("rope_pos")
            if isinstance(rope, dict) and "x" in rope:
                answer = (float(rope["x"]), "recorded rope")
            else:
                hints: list[float] = []
                for point in layer.get("jump_points", []) or []:
                    if isinstance(point, dict) and "x" in point:
                        try:
                            hints.append(float(point["x"]))
                        except (TypeError, ValueError):
                            continue
                if hints:
                    target = min(
                        hints, key=lambda value: abs(value - self.fixed_target_x)
                    )
                    answer = (
                        target,
                        "has no recorded rope; using its recorded jump point "
                        f"x={target:.6f} as the rope approach",
                    )
                else:
                    answer = (
                        None,
                        "has no recorded rope and no jump point to approach it "
                        f"(the legacy rope x={self.fixed_target_x:.3f} is not "
                        "used on a floor that never recorded one)",
                    )
        self._note_rope_target_answer(floor, answer)
        return answer

    def _note_rope_target_answer(
        self, floor: str, answer: tuple[Optional[float], str]
    ) -> None:
        """Log a rope-target answer once per floor and answer, not per frame."""

        value, note = answer
        text = f"x={value:.6f} ({note})" if value is not None else note
        if self._rope_target_answers.get(floor) == text:
            return
        self._rope_target_answers[floor] = text
        if value is None:
            LOG.warning("ROPE TARGET: %s %s", floor, note)
        elif note != "recorded rope":
            LOG.info("ROPE TARGET: %s %s", floor, note)

    def _route_target(
        self, observation: MinimapObservation
    ) -> tuple[Optional[float], bool, str]:
        """Log every route-state change before returning the route decision.

        Observed failure: patrol is armed but the character stands still and
        no movement line reaches the log at all.  The state name (for example
        ``patrol-paused`` / ``stand-still`` / ``return-climb-waiting``) is the
        difference between a wedged worker thread and a state that simply
        produced no key this frame, so it is logged on change only.
        """

        target, near_target, label = self._route_target_impl(observation)
        if label != self._route_target_label:
            self._route_target_label = label
            LOG.info(
                "route state: %s (target_x=%s climbing=%s patrol_enabled=%s "
                "return_mode=%s layer=%s)",
                label,
                target,
                near_target,
                self.patrol_enabled,
                self._return_mode,
                (self._route_layers[self._route_layer_index]
                 if (self._route_layer_index is not None
                     and 0 <= self._route_layer_index < len(self._route_layers))
                 else None),
            )
        return target, near_target, label

    def _route_target_impl(
        self, observation: MinimapObservation
    ) -> tuple[Optional[float], bool, str]:
        """Return target X, whether near-target means climb, and route label.

        Left/Rope/Right are independent actions: the layer patrols exactly
        the recorded subset (in left -> right -> rope order).  With nothing
        recorded the worker stands still (``stand-still``) and only attacks.
        """

        if not self.patrol_enabled:
            return None, False, "patrol-paused"
        if self.stationary_attack_enabled:
            if self._stationary_attack_anchor is None:
                return None, False, "stationary-attack-awaiting-anchor"
            if self._stationary_return_route_ready:
                # A normal patrol route is used only after the marker leaves
                # the anchor layer.  While standing on it, the temporary X/Y
                # point continues to be the sole horizontal target.
                if self._return_mode is None:
                    self._begin_stationary_return(observation)
                if self._return_mode is None:
                    return self._stationary_attack_anchor.x, False, "stationary-attack"
            else:
                return self._stationary_attack_anchor.x, False, "stationary-attack"
        if observation.player is None:
            return None, False, "waiting-marker"
        if self._return_mode == "drop-to-route":
            return None, False, "drop-to-route"
        if self._return_mode == "climb-to-route":
            # Return climb: walk to and climb the CURRENT floor's own rope
            # (the floor is below the patrol range).  When the climb lands
            # on the next floor ``_run_climb_step`` re-detects and either
            # restarts patrol or keeps climbing.
            detected_floor = self._detect_stationary_return_floor(
                observation
            ) if self.stationary_attack_enabled else self._detect_floor_all(observation)
            climb_in_progress = bool(
                self._climb_state.up_held
                or self._climb_state.phase != "idle"
            )
            # If a rope attempt drops the character to a lower recorded
            # floor, the old rope is no longer reachable.  Rebase the return
            # there before evaluating a normal upward-arrival confirmation.
            # This keeps a failed layer2 climb from looping forever after the
            # marker has already settled on layer1.
            if (climb_in_progress
                    and self._restart_return_climb_after_descent(
                        detected_floor, observation
                    )):
                return self._route_target(observation)
            # A noisy floor reading while simply walking to the rope must not
            # be mistaken for an arrival.  It previously changed both the
            # rope X target and the latent patrol phase before any climb had
            # happened, causing the visible left/right swing after a failed
            # rope attempt.  Only an active climb may confirm a new floor.
            if (climb_in_progress
                    and self._return_climb_arrival_ready(detected_floor)):
                floor = self._return_arrival_floor
                assert floor is not None
                LOG.info(
                    "RETURN TO ROUTE: climb settled on %s; restarting patrol",
                    floor,
                )
                self._finish_return(floor, observation)
                return self._route_target(observation)
            # Keep the rope of the confirmed departing floor until the climb
            # arrival has been confirmed above.  A single early target-layer
            # marker sample must never redirect this climb to that target
            # layer's rope (the observed layer1 -> layer2 return switched
            # 0.620301 to 0.500000 and could no longer finish).
            floor = self._return_from_floor
            if floor is None:
                return None, False, "return-climb-waiting"
            rope_x, _rope_note = self._rope_target_for_floor(floor)
            if rope_x is None:
                # Refuse to press a climb toward the legacy global rope X on a
                # floor that never recorded a rope: on the operator's layer2 that
                # walk to x=0.5 went off the platform and dropped the character
                # to layer1.  Standing still is the honest answer; the
                # de-duplicated warning above names what is missing.
                return None, False, "return-climb-no-rope"
            return rope_x, True, "return.climb"
        if not self._route_layers:
            # Nothing recorded on any layer: stand still (Fixed Attack / YOLO
            # keep attacking) instead of the old fall-back-to-rope walk.  An
            # active stand-still return above is deliberately handled first:
            # it can use a lower layer's rope even when that layer has no
            # patrol edges and therefore is absent from ``_route_layers``.
            return None, False, "stand-still"
        self._select_route_layer(observation)
        if self._route_layer_index is None or self._route_layer_index >= len(self._route_layers):
            return None, False, "route-complete"
        name = self._route_layers[self._route_layer_index]
        layer = self.important_positions[name]
        phases = self._layer_phases(name)
        if not phases:
            return None, False, f"{name}.stand-still"
        # Reconcile the current phase with what this layer actually recorded
        # (a UI edit may have removed the phase's point, or the route reset).
        if self._route_phase not in phases and self._route_phase != "drop":
            LOG.info("route phase %r not recorded on %s; starting at %s",
                     self._route_phase, name, phases[0])
            self._route_phase = phases[0]
        if self._route_phase == "left":
            return float(layer["left_most_pos"]["x"]), False, f"{name}.left-most"
        if self._route_phase == "right":
            return float(layer["right_most_pos"]["x"]), False, f"{name}.right-most"
        if self._route_phase == "drop":
            return None, False, f"{name}.drop-to-first"
        is_final = self._route_layer_index == len(self._route_layers) - 1
        if is_final:
            # The final layer has no rope to climb: loop its own actions, or
            # drop to the first layer when that is the configured end action
            # (an explicitly configured patrol floor range always drops back
            # to its first floor — e.g. range [layer2, layer3] drops from
            # layer3 back to layer2 once layer3's patrol finishes).
            if (len(self._route_layers) > 1
                    and (self._patrol_range_configured
                         or self.final_layer_action == "drop_to_first_layer")):
                return None, False, f"{name}.drop-to-first"
            self._route_phase = phases[0]
            return self._route_target(observation)
        if not self.climbing_enabled:
            return None, False, "route-complete"
        rope = layer.get("rope_pos", {})
        rope_x = float(rope["x"]) if isinstance(rope, dict) and "x" in rope else self.fixed_target_x
        return rope_x, True, f"{name}.rope"

    def _sync_patrol_controller(
        self, coordinate_layout: Optional[CoordinateLayout] = None
    ) -> None:
        if self.patrol_controller is None:
            return
        snapshot = self.patrol_controller.snapshot(coordinate_layout)
        previous_name = None
        if (self._route_layer_index is not None
                and 0 <= self._route_layer_index < len(self._route_layers)):
            previous_name = self._route_layers[self._route_layer_index]
        # The route is every action-bearing layer (any subset of Left/Rope/
        # Right), in recorded bottom-up order plus any newly recorded layers.
        routable = {
            name for name, layer in snapshot.layers.items()
            if _layer_present_actions(layer)
        }
        new_route = [name for name in snapshot.route_order if name in routable]
        extras = sorted(
            routable - set(new_route),
            key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
        )
        new_route.extend(extras)
        # The route must stay bottom-up by numeric suffix (never recording or
        # route_order order): a lower layer recorded later would otherwise
        # become the "final" layer, hide its rope, and strand the character.
        new_route.sort(
            key=lambda name: int("".join(filter(str.isdigit, name)) or 0)
        )
        # Apply the contiguous patrol floor range selected in the UI: patrol
        # only floors in [patrol_start_layer .. patrol_end_layer]; a floor
        # outside the range makes the character return to it (falling
        # recovery / return-to-route) instead of patrolling there.
        new_route = _slice_patrol_range(
            new_route,
            snapshot.patrol_start_layer or None,
            snapshot.patrol_end_layer or None,
        )
        self._patrol_range_min, self._patrol_range_max = _patrol_range_numbers(
            new_route,
            snapshot.patrol_start_layer or None,
            snapshot.patrol_end_layer or None,
        )
        # Explicitly configured range (both bounds selected in the UI): its
        # TOP floor drops back to its FIRST floor after the patrol finishes
        # there, looping the range instead of repeating the top floor forever.
        self._patrol_range_configured = bool(snapshot.patrol_range_set)
        canonical_start, _canonical_end = _canonical_patrol_range(
            snapshot.patrol_start_layer or None, snapshot.patrol_end_layer or None
        )
        self.first_layer = canonical_start or (
            new_route[0] if new_route else self.first_layer
        )
        route_changed = new_route != self._route_layers
        self.patrol_enabled = snapshot.enabled
        self.climbing_enabled = snapshot.climbing_enabled
        self.final_layer_action = snapshot.final_layer_action
        self.important_positions = snapshot.layers
        if route_changed:
            self._route_layers = new_route
            if previous_name in new_route:
                self._route_layer_index = new_route.index(previous_name)
            else:
                self._route_layer_index = None
                self._route_phase = "left"
                self._route_patrol_cycle = 1
            LOG.info("patrol route updated from UI: %s",
                     " -> ".join(new_route) if new_route else "none")

    def _layer_phases(self, name: str) -> list[str]:
        """Ordered action phases for a layer from its recorded points.

        Left -> Right -> Rope, but only the actions actually recorded.  The
        final layer's rope is omitted (the snapshot already drops it), so a
        rope-only layer climbs straight to its rope and a layer with only
        Left/Right patrols just those.
        """

        layer = self.important_positions.get(name, {})
        phases: list[str] = []
        if not isinstance(layer, dict):
            return phases
        if isinstance(layer.get("left_most_pos"), dict):
            phases.append("left")
        if isinstance(layer.get("right_most_pos"), dict):
            phases.append("right")
        is_final = (
            self._route_layer_index is not None
            and self._route_layer_index == len(self._route_layers) - 1
        )
        if isinstance(layer.get("rope_pos"), dict) and self.climbing_enabled and not is_final:
            phases.append("rope")
        return phases

    def _advance_route_endpoint(self, observation: MinimapObservation, target_x: Optional[float]) -> bool:
        # A below-range recovery has its own rope target and completion
        # rules.  The route index still describes the old patrol floor while
        # ``return.climb`` is walking toward that rope, so advancing its
        # stored Left/Right phase here creates a phantom endpoint reversal:
        # the character swings across the platform instead of retrying the
        # same rope.  Return handling alone is allowed to change route state.
        if self._return_mode is not None:
            return False
        if observation.player is None or target_x is None:
            self._clear_endpoint_band_timeout()
            return False
        if self._route_layer_index is None or self._route_layer_index >= len(self._route_layers):
            return False
        name = self._route_layers[self._route_layer_index]
        is_final = self._route_layer_index == len(self._route_layers) - 1
        forced_entry = self._forced_phase_entry
        if (forced_entry is not None
                and forced_entry[:2] == (
                    self._route_layer_index, self._route_phase
                )):
            entry_x = forced_entry[2]
            moved_away = (
                observation.player.x < entry_x - self._current_horizontal_tolerance
                if self._route_phase == "left" else
                observation.player.x > entry_x + self._current_horizontal_tolerance
            )
            if not moved_away:
                # The previous endpoint was unreachable. The forced reverse
                # must visibly start before its endpoint can advance; without
                # this, an old position can skip Left and retry blocked Right.
                return False
            self._forced_phase_entry = None
            # Let the next fresh frame evaluate the endpoint. This avoids one
            # marker sample both proving departure and completing the new phase.
            return False
        if (forced_entry is not None
                and forced_entry[:2] != (
                    self._route_layer_index, self._route_phase
                )):
            self._forced_phase_entry = None
        if self._route_phase in ("left", "right") and self._endpoint_no_progress(
                observation.player.x, target_x, self._route_phase):
            self._force_advance_phase(observation.player.x)
            return True
        if self._route_phase == "left":
            # Left and right arrivals use the SAME band.  The former 0.25x
            # left band (~0.2 px on the minimap) could never be satisfied when
            # the saved left-most point sat just outside the walkable platform
            # (edge, or a residual recording error), so the character walked
            # into the edge until the self-rescue restarted the patrol.
            if (observation.player.x
                    > target_x + self._current_horizontal_tolerance):
                if self._endpoint_band_timeout(
                        observation.player.x - target_x,
                        self._current_horizontal_tolerance, target_x):
                    self._force_advance_phase(observation.player.x)
                    return True
                return False
            self._clear_endpoint_band_timeout()
        elif self._route_phase == "right":
            if observation.player.x < target_x - self._current_horizontal_tolerance:
                if self._endpoint_band_timeout(
                        target_x - observation.player.x,
                        self._current_horizontal_tolerance, target_x):
                    self._force_advance_phase(observation.player.x)
                    return True
                return False
            self._clear_endpoint_band_timeout()
        else:
            # Rope phase advances through the climb machinery, not here.
            self._clear_endpoint_band_timeout()
            return False
        phases = self._layer_phases(name)
        current = self._route_phase
        if self._repeat_patrol_cycle_if_needed(name, phases, current):
            return True
        if current in phases:
            index = phases.index(current)
            if index + 1 < len(phases):
                self._route_phase = phases[index + 1]
                LOG.info("route endpoint reached/crossed: %s; next %s",
                         current, phases[index + 1])
                return True
        # End of this layer's recorded actions.
        if (is_final and len(self._route_layers) > 1
                and (self._patrol_range_configured
                     or self.final_layer_action == "drop_to_first_layer")):
            self._route_phase = "drop"
            LOG.info("final layer patrol done; dropping to first layer%s",
                     " (range top; dropping back to range first)"
                     if self._patrol_range_configured else "")
            return True
        if phases:
            # Repeat the layer's own actions: stand at a lone point, patrol a
            # floor back-and-forth, or hold position before climbing.
            self._route_phase = phases[0]
            LOG.info("route endpoint reached/crossed: %s; repeating %s",
                     current, phases[0])
            return True
        self._route_phase = "stand"
        return True

    def _repeat_patrol_cycle_if_needed(
        self, name: str, phases: list[str], current: str
    ) -> bool:
        """Repeat a layer's horizontal actions before rope or drop.

        One cycle is the recorded Left and/or Right sequence. Rope-only and
        stand-still layers have no horizontal cycle and keep their existing
        behavior.
        """

        movement_phases = [phase for phase in phases if phase in ("left", "right")]
        if (not movement_phases
                or current != movement_phases[-1]
                or self._route_patrol_cycle >= self.patrol_cycles_per_layer):
            return False
        completed = self._route_patrol_cycle
        self._route_patrol_cycle += 1
        self._route_phase = movement_phases[0]
        LOG.info(
            "layer %s patrol cycle %d/%d complete; repeating from %s",
            name,
            completed,
            self.patrol_cycles_per_layer,
            self._route_phase,
        )
        return True

    def _on_first_layer(self, observation: MinimapObservation) -> bool:
        """True when the marker has reached the first (bottom) layer.

        A clear nearest marker base wins. Genuinely tied/aliased marker bands
        use scroll-compensated world Y. A marker slightly below every band is
        accepted as the bottom floor because dropping farther is impossible.
        This keeps stale world tracking from overriding a visible upper floor
        while preserving the older below-band landing tolerance.
        """

        if observation.player is None or self.first_layer is None:
            return False
        layer = self.important_positions.get(self.first_layer)
        if not _has_layer_y_supporter(layer):
            return False
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        marker_arrived = bool(
            band is not None and observation.player.y >= band[0] - 1e-9
        )
        # A scrolling minimap can keep the yellow marker at exactly the same
        # screen Y on several floors. In that layout the old broad
        # "at-or-below first layer" check was already true on the final
        # layer, so the drop phase ended before its first Alt+Down chord and
        # the route was reset to the lower floor forever. Marker Y remains
        # preferred when it identifies the first floor alone (which also
        # handles a stale world tracker), but an overlapping upper-floor band
        # must be disambiguated by scroll-compensated world Y.
        marker_candidates = _layer_y_candidates(
            observation.player.y, self.important_positions
        )
        if marker_candidates:
            # The distance is measured the same way as the ordering in ``_layer_y_candidates`` (nearest
            # RECORDED position), never from the legacy ``layer_y``: two rankings of the same question
            # must not disagree, or the drop ends on a floor the rest of the worker calls another one.
            distances = []
            for name in marker_candidates:
                candidate = self.important_positions.get(name, {})
                distance = _layer_y_distance(candidate, observation.player.y)
                if distance is not None:
                    distances.append((distance, name))
            if not distances:
                return marker_arrived
            nearest_distance = min(distance for distance, _name in distances)
            nearest = [
                name for distance, name in distances
                if abs(distance - nearest_distance) <= 1e-6
            ]
            if len(nearest) == 1:
                # The field failure had layer2's bench band overlapping the
                # exact layer3 base. The closest recorded base is layer3, so
                # stale world Y must not end the drop before Alt+Down is sent.
                return nearest[0] == self.first_layer
        elif marker_arrived:
            # The bottom floor may render a few pixels below its recorded
            # band. With no recorded upper-floor candidate there, accept it.
            return True
        else:
            # The marker matches NO floor and is not at/below the first floor's band: the character is in
            # the air on its way down (or standing on a spot no recording covers), so the descent is NOT
            # finished.  This must not fall through to the world signal: his 14:38 log had the restart fire
            # at marker_y 0.481707 while the character was still falling from layer3 (the marker ran
            # 0.372 -> 0.397 -> 0.409 -> 0.445 -> 0.482), and the raw world tracker - which swings while
            # falling - happened to sit inside layer1's recorded world band, so the drop was declared
            # arrived and the patrol restarted; the character then landed on layer2.  His layer1 world
            # anchors make that band span from 0.29 to 1.04 (canonical layer_world_y 2.416667, but the
            # points' own observed_world_y are ~1.04), so a mid-air reading can easily land in it.
            return False
        # Fall back to the world-Y signal only when marker bases are genuinely
        # tied/aliased and the structure tracker is available and confident (the
        # branches above only fall through here with a real tie between floors).
        if (observation.world_y_diamonds is not None
                and "layer_world_y" in layer
                and observation.structure_confidence >= 0.12):
            world_layers = {
                name: candidate
                for name, candidate in self.important_positions.items()
                if isinstance(candidate, dict) and "layer_world_y" in candidate
            }
            return detect_layer_by_world_y(
                observation.world_y_diamonds, world_layers
            ) == self.first_layer
        return False

    def _final_drop_arrived(self, observation: MinimapObservation) -> bool:
        """Require at least one real drop chord before accepting arrival."""

        if self._last_drop_attempt == float("-inf"):
            return False
        return self._on_first_layer(observation)

    def _reset_route_loop(self) -> None:
        """Start a fresh loop on the first layer after the planned descent landed.

        This is a fresh patrol start exactly like ``_apply_pending_patrol_start``, but it used to
        initialize far less than that path did: the fall-detection state, the return-to-route state and
        the layer-resync candidate all survived the descent.  The stale fall then resolved on the next
        frames and restarted the patrol on the floor the character had come FROM (his report: the
        character dropped back to the route's first layer but the patrol restarted on layer2), and a
        stale return mode would have kept the new loop out of the route.  Operator report on the
        "back to layer 1 (the patrol route's first layer)" procedure: it never initialized the fall-down
        logic nor the back-to-patrol-route logic.
        """

        self._release_climb_up()
        self._route_layer_index = self._route_layers.index(self.first_layer)
        # The final drop landed back on the (in-range) loop floor: a below-
        # range rescue streak is over.
        self._rescue_cycles = 0
        self._climb_lateral_streak = 0
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._forced_phase_entry = None
        self._climb_state = ClimbState()
        self._last_drop_attempt = float("-inf")
        self._descending_to_first = False
        self._descending_since = None
        self._drop_descent_saw = None
        # The new loop owns the vertical movement again: no fall, no return, no half-finished resync may
        # cross this boundary (see ``_reset_fall_tracking``).
        self._reset_fall_tracking()
        self._return_mode = None
        self._return_from_floor = None
        self._return_arrival_floor = None
        self._clear_layer_resync_candidate()
        self._aligned_frames = 0
        self._rope_approach_direction = None
        self._rope_attempted = False
        # A fresh loop re-locks the rope target: the previous loop's locked X belongs to a route state
        # that no longer exists (same reason as in ``_apply_pending_patrol_start``).
        self._held_rope_target = None
        self._climb_direction_log = None
        for event in (
            self.climbing_active_event,
            self.near_rope_event,
            self.moving_active_event,
            self.direction_transition_event,
        ):
            if event is not None:
                event.clear()
        if self.dropping_active_event is not None:
            # The drop phase is OVER (layer1 reached, new loop starts): the
            # dropping flag must clear, otherwise the patrol reports busy
            # forever and the YOLO attack stays blocked ("attack blocked:
            # patrol climbing/dropping" -> the character never attacks).
            self.dropping_active_event.clear()
        LOG.info("returned to %s; starting new patrol loop", self.first_layer)

    def _endpoint_band_timeout(
        self, distance: float, band: float, target_x: float
    ) -> bool:
        """True when a near-but-unreachable endpoint must count as reached.

        Observed failure: the character reaches the saved left-most point but
        never turns right - it keeps walking into the edge, the marker
        freezes, and the self-rescue restarts the whole patrol ("stuck, then
        the patrol restarted and he moves again").  A saved endpoint that
        renders just outside the walkable platform is unreachable by
        definition, so after ENDPOINT_ARRIVAL_TIMEOUT_FRAMES frames spent
        within a few times the arrival band the endpoint is treated as
        reached and the phase turns.
        """

        if (self._attack_state is not None
                and self._attack_state.is_active()):
            # A fight pauses the patrol by design; the marker standing still
            # near the endpoint proves nothing about reachability.
            self._marginal_endpoint_frames = 0
            return False
        key = (
            self._route_layer_index,
            self._route_phase,
            round(float(target_x), 6),
        )
        if distance > band * ENDPOINT_ARRIVAL_NEAR_MARGIN:
            # Still walking toward the endpoint: no stall to measure.
            self._marginal_endpoint_key = key
            self._marginal_endpoint_frames = 0
            return False
        if key != self._marginal_endpoint_key:
            self._marginal_endpoint_key = key
            self._marginal_endpoint_frames = 0
        self._marginal_endpoint_frames += 1
        if self._marginal_endpoint_frames < ENDPOINT_ARRIVAL_TIMEOUT_FRAMES:
            return False
        self._marginal_endpoint_frames = 0
        LOG.warning(
            "endpoint unreachable on %s: %s at distance=%.6f outside the "
            "%.6f arrival band for %d frames; treating it as reached",
            (self._route_layers[self._route_layer_index]
             if (self._route_layer_index is not None
                 and 0 <= self._route_layer_index < len(self._route_layers))
             else None),
            self._route_phase,
            distance,
            band,
            ENDPOINT_ARRIVAL_TIMEOUT_FRAMES,
        )
        return True

    def _clear_endpoint_band_timeout(self) -> None:
        """Forget the marginal-endpoint stall counter."""

        self._marginal_endpoint_key = None
        self._marginal_endpoint_frames = 0

    def _endpoint_no_progress(
        self, player_x: float, target_x: float, phase: str
    ) -> bool:
        """True when a Left/Right phase stops closing on its endpoint.

        Covers the far case the near-band timeout cannot see (for example a
        saved endpoint beyond a wall, or a marker frozen by a movement-locking
        buff): the walk holds its key into the edge forever and the patrol
        looks broken ("keeps walking left").  A real walk always reduces the
        distance, so only a run of frames without progress counts, and attack
        pauses are ignored.
        """

        if self._attack_state is not None and self._attack_state.is_active():
            self._clear_endpoint_progress()
            return False
        key = (
            self._route_layer_index,
            str(phase),
            round(float(target_x), 6),
        )
        distance = abs(float(player_x) - float(target_x))
        if key != self._progress_key:
            self._progress_key = key
            self._progress_best = distance
            self._progress_frames = 0
            return False
        if distance < self._progress_best - 1e-9:
            self._progress_best = distance
            self._progress_frames = 0
            return False
        self._progress_frames += 1
        if self._progress_frames < ENDPOINT_NO_PROGRESS_FRAMES:
            return False
        self._clear_endpoint_progress()
        LOG.warning(
            "endpoint %s unreachable on %s: no progress toward x=%.6f for %d "
            "frames (distance=%.6f); treating it as reached",
            phase,
            (self._route_layers[self._route_layer_index]
             if (self._route_layer_index is not None
                 and 0 <= self._route_layer_index < len(self._route_layers))
             else None),
            target_x,
            ENDPOINT_NO_PROGRESS_FRAMES,
            distance,
        )
        return True

    def _clear_endpoint_progress(self) -> None:
        """Forget the endpoint progress tracker."""

        self._progress_key = None
        self._progress_best = 0.0
        self._progress_frames = 0

    def _force_advance_phase(self, player_x: Optional[float] = None) -> None:
        """Boundary unreachable (walk blocked / out-of-bounds target): the
        character is AT the reachable boundary - complete the current phase
        and move to the next recorded one, breaking the loop of chasing an
        unreachable target forever."""
        if (self._route_layer_index is None
                or self._route_layer_index >= len(self._route_layers)):
            return
        name = self._route_layers[self._route_layer_index]
        phases = self._layer_phases(name)
        current = self._route_phase

        def arm_reversal_guard() -> None:
            if self._route_phase in ("left", "right") and player_x is not None:
                self._forced_phase_entry = (
                    self._route_layer_index, self._route_phase, float(player_x)
                )
            else:
                self._forced_phase_entry = None

        if self._repeat_patrol_cycle_if_needed(name, phases, current):
            arm_reversal_guard()
            LOG.warning(
                "boundary %s unreachable on %s; starting patrol cycle %d/%d",
                current,
                name,
                self._route_patrol_cycle,
                self.patrol_cycles_per_layer,
            )
            return
        if current in phases:
            index = phases.index(current)
            if index + 1 < len(phases):
                self._route_phase = phases[index + 1]
                arm_reversal_guard()
                LOG.warning("boundary %s unreachable on %s; forcing next "
                            "phase %s", current, name, phases[index + 1])
                return
        if phases:
            self._route_phase = phases[0]
            arm_reversal_guard()
            LOG.warning("boundary %s unreachable on %s; looping %s",
                        current, name, phases[0])

    def _climb_cycle_failed(self) -> bool:
        """Count consecutive failed climb cycles at the rope.

        After ``climb_failed_cycles_reset`` full failed cycles (both jump
        directions tried, correction already used) the route restarts at
        left-most: the character walks away from the rope and re-approaches
        it from the edge, where the directional jump grabs reliably.  Without
        this a character stuck under a rope the straight jump cannot reach
        would jump in place forever and never patrol.

        When the marker X is FROZEN across cycle failures (the directional
        jump does not move the character toward the rope at all - a wall or
        platform edge blocks it), the rope is unreachable from this approach:
        escalate straight to the self-rescue instead of waiting for restarts.
        """

        self._climb_failures += 1
        if self._climb_failures < self.climb_failed_cycles_reset:
            return False
        self._climb_failures = 0
        self._escalate_failed_climb_approach()
        return True

    def _escalate_failed_climb_approach(self) -> None:
        """One full failed rope approach: restart the route at left-most.

        The character walks away from the rope and re-approaches it from the
        platform edge, where the directional jump grabs reliably.  Physical
        Up is released FIRST: the failure can arrive right after a sideways
        climb jump that still owns the Up key, and wiping the climb state
        without releasing it would leave the character walking with Up held
        forever.  Repeated restarts (or a frozen marker X = the rope is
        unreachable from this side) escalate to the full self-rescue.
        """
        self._release_climb_up()
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._climb_state = ClimbState()
        self._climb_restarts += 1
        LOG.warning(
            "CLIMB failed at the rope; restarting patrol from left-most "
            "(restart %d)",
            self._climb_restarts,
        )
        # X 冻结检测：跳向绳子的过程中标记 X 纹丝不动 → 绳子从这一侧
        # 够不到（墙/平台边缘挡住），别等 4 次重启，直接升级自救。
        frozen_x = False
        if self.last_observation is not None and self.last_observation.player is not None:
            x = self.last_observation.player.x
            if (self._climb_last_x is not None
                    and abs(x - self._climb_last_x) < 0.001):
                frozen_x = True
            self._climb_last_x = x
        if self._climb_restarts >= 4 or frozen_x:
            # 同一层爬楼反复失败 / X 冻结（绳子不可达）：升级为完整自救
            # （回第一层 + 重启巡逻 + 重锚定世界Y）。
            LOG.warning(
                "CLIMB keeps failing (%d restarts, frozen_x=%s); self-rescue: "
                "drop to layer1 and restart patrol",
                self._climb_restarts, frozen_x,
            )
            self._climb_restarts = 0
            self._climb_last_x = None
            self._trigger_rescue()
        return True

    def _climb_cycle_reset(self) -> None:
        self._climb_failures = 0
        self._climb_restarts = 0
        self._climb_last_x = None

    def _advance_after_climb(self) -> None:
        assert self._route_layer_index is not None
        self._route_layer_index += 1
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._climb_state = ClimbState()
        self._patrol_busy_until = 0.0
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self._route_layer_index < len(self._route_layers):
            LOG.info("climb verified; starting %s patrol", self._route_layers[self._route_layer_index])
        else:
            LOG.info("climb verified; waiting for next layer calibration")

    def _next_layer_reached(self, observation: MinimapObservation) -> bool:
        if (observation.player is None or self._route_layer_index is None
                or self._route_layer_index + 1 >= len(self._route_layers)):
            return False
        next_name = self._route_layers[self._route_layer_index + 1]
        layer = self.important_positions[next_name]
        if (observation.world_y_diamonds is not None
                and "layer_world_y" in layer
                and observation.structure_confidence >= 0.12):
            world_tol = float(layer.get("world_y_tolerance", 0.75))
            world_band = _layer_world_y_band(layer, world_tol)
            return bool(
                world_band is not None
                and world_band[0] - 1e-9 <= observation.world_y_diamonds
                <= world_band[1] + 1e-9
            )
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        return bool(
            band is not None
            and band[0] - 1e-9 <= observation.player.y <= band[1] + 1e-9
        )

    def _release_climb_up(self) -> None:
        if self._climb_state.up_held:
            force_key_up = getattr(self.key_sender, "force_key_up", None)
            key_up = getattr(self.key_sender, "key_up", None)
            if callable(force_key_up):
                force_key_up("up", reason="climb release")
            elif key_up is not None:
                key_up("up")
            self._climb_state.up_held = False

    def _yield_climb_to_jump_point(self) -> None:
        """Stop ordinary rope recovery without releasing a jump-point's Up.

        A directional jump point sends ``direction + Alt`` while holding Up
        first.  The normal rope planner used to see the same rope on the next
        capture, open its own climb state, and retry with Alt+Left/Right based
        on the tiny rope-X gap.  That side correction is valid for a missed
        *ordinary* rope jump, but it steals a recorded jump-point climb.

        ``key_up`` releases only the climb state's ownership reference.  It
        deliberately does not use ``force_key_up``: the jump-point session
        still owns Up until its landing-Y settle detector finishes.
        """

        state = self._climb_state
        if state.up_held:
            key_up = getattr(self.key_sender, "key_up", None)
            if callable(key_up):
                key_up("up")
        if state.phase != "idle" or state.up_held:
            LOG.info(
                "JUMP POINT climb ownership: cancelled ordinary rope recovery; "
                "keeping jump-point Up held"
            )
        self._climb_state = ClimbState()
        # The jump point still owns the vertical session, therefore this gate
        # must stay set until _update_jump_point_landing clears it.
        if self.climbing_active_event is not None:
            self.climbing_active_event.set()

    def _sync_input_session(self) -> None:
        """Drop worker-local holds when the sender starts a new generation."""

        getter = getattr(self.key_sender, "input_session", None)
        if not callable(getter):
            return
        try:
            current = int(getter())
        except Exception:
            LOG.debug("could not read input session", exc_info=True)
            return
        if self._input_session_seen is None:
            self._input_session_seen = current
            return
        if current == self._input_session_seen:
            return
        previous = self._input_session_seen
        self._input_session_seen = current
        with self._direction_lock, self._hold_lock:
            self._walk_hold_key = None
            self._walk_hold_z = False
            self._walk_hold_until = 0.0

    def perform_micro_step(self) -> bool:
        """Run one short Left/Right step with two attacks between directions.

        Called only by ``MotionArbiter``.  The arbiter has already blocked
        attack and other queued motions; this method clears the current patrol
        walk, then owns ``first direction -> attack twice -> second
        direction``. The final direction is chosen from the stand-still 朝向
        setting or the prior patrol direction, so the sequence preserves the
        intended facing.
        """

        if not self._patrol_input_allowed():
            return False
        if not _sender_is_safe(self.key_sender):
            LOG.info("small-step blocked: target window is not safely selected")
            return False
        if self._movement_busy_now():
            LOG.info("small-step skipped: climb/drop input is active")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            return False
        # Check and claim under the same directional lock.  A climb/drop
        # cannot begin between this busy check and the first tiny step.
        with self._direction_lock, self._hold_lock:
            if not self._patrol_input_allowed():
                return False
            if self._movement_busy_now():
                LOG.info("small-step skipped: climb/drop input is active")
                return False
            # Interrupt patrol cleanly.  This action owns only Left/Right:
            # never force-release Up/Down, Alt, or any unrelated key.  The
            # arbiter plus this directional lock keep jump/buff/climb/drop
            # transactions outside this atomic two-step sequence.
            # In stand-still mode the operator's 朝向 selection is the facing
            # source of truth.  On ordinary patrol, preserve the direction
            # that was active before the asynchronous walk hold was released.
            if self.stationary_attack_enabled:
                resume_direction = self._stationary_facing_target_for_frame()
            else:
                resume_direction = self._walk_hold_key
                if resume_direction not in ("left", "right"):
                    resume_direction = None
            self._release_walk_hold()
            is_key_down = getattr(self.key_sender, "is_key_down", None)
            if callable(is_key_down) and (
                    is_key_down("up") or is_key_down("down")):
                LOG.info("small-step skipped: vertical movement is active")
                return False
            if self.moving_active_event is not None:
                self.moving_active_event.clear()
            # The first micro-step deliberately points away from the facing
            # target, then the second returns to it: 朝向左 is Right -> Left;
            # 朝向右 is Left -> Right.  Outside stand-still mode, the prior
            # patrol direction remains the target where one exists.
            final_direction = resume_direction or "right"
            first = "right" if final_direction == "left" else "left"
            second = final_direction
            first_claimed = key_down(first) is not False
            if not first_claimed:
                return False
            try:
                if not self._wait_for_patrol_motion(0.10):
                    return False
            finally:
                key_up(first)
            # The middle attack is part of this exclusive arbiter motion.
            # Keep the opposite direction out of the game's post-cast window:
            # Maple can otherwise swallow that direction as continuation of
            # the attack animation.
            tap = getattr(self.key_sender, "tap", None)
            attack_key = str(getattr(self, "small_step_attack_key", "ctrl"))
            if not callable(tap):
                LOG.info("small-step skipped: input sender cannot tap attack key")
                return False
            if not self._patrol_input_allowed() or tap(attack_key) is False:
                return False
            if self.motion_arbiter is not None:
                note_attack = getattr(self.motion_arbiter, "note_attack", None)
                if callable(note_attack):
                    note_attack()
            # Post-attack recovery is intentionally long.  The second
            # direction is only useful after the cast animation can no longer
            # consume it as a movement input.
            if not self._wait_for_patrol_motion(0.70):
                return False
            if not self._patrol_input_allowed():
                return False
            second_claimed = key_down(second) is not False
            if not second_claimed:
                return False
            try:
                if not self._wait_for_patrol_motion(0.10):
                    return False
            finally:
                key_up(second)
        if self.stationary_attack_enabled:
            # The pair ends facing the selected 朝向, so the stand-still facing
            # obligation is satisfied and the (net zero) step is accepted.
            self._stationary_facing_command = final_direction
            self._stationary_x_settled = True
        LOG.info(
            "small-step complete: %s -> attack -> wait 0.70s -> %s; facing target=%s",
            first, second, resume_direction or "right",
        )
        return True

    def _begin_stationary_return(
        self,
        observation: MinimapObservation,
        *,
        after_confirmed_fall: bool = False,
    ) -> None:
        """Use the patrol climb state to return to a temporary 站桩 anchor.

        The anchor is deliberately a temporary point, while the layer/rope
        records remain the existing patrol records.  An anchor-layer reading
        is the only settled state; every lower or higher matched layer is a
        route-recovery state.
        """

        if (not self._stationary_return_route_ready
                or self._return_mode is not None
                or self._jump_point_up_held
                or observation.player is None):
            return
        anchor_layer = self._stationary_route_anchor_layer
        if not anchor_layer:
            return
        floor = self._detect_stationary_return_floor(
            observation, after_confirmed_fall=after_confirmed_fall
        )
        if floor is None or floor == anchor_layer:
            return
        # Route return owns movement from this point.  Do not let a due
        # pickup circuit inherit the former settled-at-stake state while the
        # character is on a lower/higher floor.
        self._stationary_x_settled = False
        if not self._stationary_pickup_return_active:
            self._abort_stationary_pickup_for_route_return()
        self._reanchor_tracker_to_layer(floor, observation)
        if _layer_number(floor) < _layer_number(anchor_layer):
            mode = "climb-to-route"
        else:
            mode = "drop-to-route"
        self._return_mode = mode
        self._return_from_floor = floor
        self._return_arrival_floor = None
        if floor in self._route_layers:
            # Start layer-arrival confirmation from the floor we actually
            # departed.  The next higher layer then becomes the expected
            # transition instead of an ambiguous/current-layer reading.
            self._route_layer_index = self._route_layers.index(floor)
        LOG.warning(
            "STATIONARY RETURN: left anchor layer %s for %s; %s via patrol route",
            anchor_layer, floor,
            "climbing back" if mode == "climb-to-route" else "dropping back",
        )

    def perform_arbiter_buff(self, key: str, hold_seconds: float = 0.20) -> bool:
        """Tap one queued buff after cleanly yielding patrol movement.

        This is called only by MotionArbiter.  It releases the ordinary
        Left/Right(+Z) walk under the same directional lock used by climb,
        drops and small-step, taps the buff, then leaves the next patrol frame
        free to re-arm its previous direction.  It never runs during vertical
        movement or a climb/transition.
        """

        if (not self._patrol_input_allowed()
                or not key or not _sender_is_safe(self.key_sender)):
            return False
        with self._direction_lock, self._hold_lock:
            if not self._patrol_input_allowed():
                return False
            if not self.motion_arbiter_motion_allowed():
                LOG.info("arbiter buff deferred: movement is not at a safe stage")
                return False
            self._release_walk_hold()
            if self.moving_active_event is not None:
                self.moving_active_event.clear()
            try:
                sent = self.key_sender.tap(key, hold_seconds=hold_seconds) is not False
            except Exception:
                LOG.exception("arbiter buff tap failed key=%s", key)
                return False
        if sent:
            LOG.info("arbiter buff executed: %s; patrol will resume", key)
        return sent

    def motion_arbiter_motion_allowed(self) -> bool:
        """Whether queued jump/buff/small-step input may be emitted now.

        The arbiter may act while the current frame is a regular left/right
        patrol walk or a walk toward a rope.  A started patrol with no route
        is also safe: the character stands still and may still attack/buff.
        Climb, drop, stair jump, alignment, and transition frames are
        deliberately excluded.

        This is called by ``MotionArbiter`` while it holds its condition
        lock. It must therefore read the movement snapshot lock-free: taking
        ``_direction_lock`` here creates an AB-BA deadlock with a direction
        handoff, which holds that lock while checking arbiter attack state.
        These fields are published atomically by the movement thread; a
        one-frame-old snapshot may only defer a queued motion, never change a
        direction or send a key itself.
        """
        decision = self.last_decision
        if (not _sender_is_safe(self.key_sender)
                or self._movement_busy_now()
                or (self.direction_transition_event is not None
                    and self.direction_transition_event.is_set())):
            return False
        if self.stationary_attack_enabled:
            # Stand-still attack has no climb/drop/route phase. Its anchor
            # correction is already excluded by _movement_busy_now(), so an
            # otherwise idle frame is safe for the atomic small-step/facing
            # action as well.
            return True
        if self.patrol_enabled and not self._route_layers:
            return True
        return bool(
            self._motion_arbiter_stage in ("patrol", "move-to-rope")
            and decision is not None
            and decision.key in ("left", "right")
        )
        self._climb_state = ClimbState()
        if self.pickup_active_event is not None:
            self.pickup_active_event.clear()
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        LOG.info("movement reset stale holds for input session %d -> %d",
                 previous, current)

    def _current_layer_world_y(self) -> Optional[float]:
        if (self._route_layer_index is None
                or not 0 <= self._route_layer_index < len(self._route_layers)):
            return None
        layer = self.important_positions.get(
            self._route_layers[self._route_layer_index], {}
        )
        if not isinstance(layer, dict) or "layer_world_y" not in layer:
            return None
        return float(layer["layer_world_y"])

    def _pin_stationary_layer_world_y(
        self, observation: MinimapObservation
    ) -> MinimapObservation:
        """Ignore OpenCV vertical aliases only while the floor is confirmed.

        The world-Y reading is pinned to the believed floor's canonical
        anchor ONLY while the marker Y itself still agrees with that floor's
        band (the character is visibly cruising on it).  On a scrolling
        minimap the raw marker Y is screen-relative, so after a knock-down
        it can read off-band while the character truly sits on another
        floor - pinning then would hide the landing from the world-Y
        channel, so the raw tracker reading passes through for the landing
        reconciliation to re-anchor to the true layer.
        """

        if self._climb_state.up_held or self._climb_state.phase != "idle":
            return observation
        if observation.player is None:
            return observation
        floor = self._current_route_floor()
        if floor is None:
            return observation
        layer = self.important_positions.get(floor)
        if not isinstance(layer, dict):
            return observation
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if (band is None
                or not (band[0] - 1e-9 <= observation.player.y
                        <= band[1] + 1e-9)):
            return observation
        canonical = _layer_world_anchor_at_x(
            layer, observation.player.x
        )
        if canonical is None:
            return observation
        return replace(
            observation,
            world_y_diamonds=canonical,
            structure_confidence=max(observation.structure_confidence, 1.0),
        )

    def _reanchor_tracker_to_layer(
        self,
        layer_name: str,
        observation: Optional[MinimapObservation] = None,
    ) -> None:
        layer = self.important_positions.get(layer_name, {})
        canonical = _layer_world_anchor_at_x(
            layer,
            (observation.player.x
             if observation is not None and observation.player is not None
             else None),
        )
        reanchor = getattr(self.structure_tracker, "reanchor_world_y", None)
        if canonical is not None and callable(reanchor):
            reanchor(canonical)
            return
        start_session = getattr(self.structure_tracker, "start_session", None)
        if canonical is not None and callable(start_session):
            start_session(canonical)

    def _reanchor_tracker_to_current_layer(
        self, observation: Optional[MinimapObservation] = None
    ) -> None:
        floor = self._current_route_floor()
        # During a return climb the route index can still describe the stale
        # pre-fall floor (out-of-route floors never re-index it), so the
        # "current" floor would anchor the world origin to the WRONG layer
        # (observed: world Y re-anchored to layer2 at -0.178 while the
        # character was still climbing up FROM layer1 - corrupting climb
        # verification/arrival).  Anchor to the floor the return is actually
        # climbing from: the detected floor first, then the recorded return
        # floor.
        if (floor is None
                or self._return_mode == "climb-to-route"):
            detected = (
                self._detect_floor_all(observation)
                if observation is not None else None
            )
            if detected is not None:
                floor = detected
            elif self._return_from_floor is not None:
                floor = self._return_from_floor
        if floor is not None:
            self._reanchor_tracker_to_layer(floor, observation)

    def _log_detected_layer(
        self,
        detected_layer_name: Optional[str],
        observation: MinimapObservation,
    ) -> None:
        """Log the current layer without interrupting movement analysis."""

        changed = detected_layer_name != self._debug_last_layer
        if changed:
            self._debug_last_layer = detected_layer_name
        log = LOG.info if changed else LOG.debug
        log(
            "LAYER DEBUG: %s %s (player_y=%.6f world_y=%s)",
            "now on" if changed else "on",
            detected_layer_name or "none",
            (observation.player.y if observation.player is not None
             else float("nan")),
            (f"{observation.world_y_diamonds:.6f}"
             if observation.world_y_diamonds is not None else "n/a"),
        )

    def run(self) -> None:
        LOG.info("movement worker started (%s)", "DRY-RUN" if getattr(self.key_sender, "dry_run", True) else "LIVE")
        # 独立 hold 管理线程：主循环处理帧时方向键由它按/松。
        threading.Thread(target=self._hold_manager, name="walk-hold",
                         daemon=True).start()
        threading.Thread(target=self._stall_watchdog, name="movement-watchdog",
                         daemon=True).start()
        while not self.stop_event.is_set():
            self._last_frame_at = time.monotonic()
            try:
                frame = self.frame_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._sync_input_session()
                # 自动重连 arms one route check; it must be observed even while the automation
                # gate below keeps this worker stood down for the whole reconnect sequence.
                self._track_reconnect_window(time.monotonic())
                if (self.automation_active_event is not None
                        and not self.automation_active_event.is_set()):
                    if self._automation_was_active:
                        # Patrol just stopped / input disarmed. The central
                        # StatusWorker owns the single game-side scrub. Do NOT
                        # inject another delayed key-up here: by the time this
                        # capture arrives the operator may already be holding
                        # Left/Right manually after Ctrl+`, and a second worker
                        # release would cancel that real key-down.
                        self._automation_was_active = False
                    # Forget local claims only. ``disable_input`` advanced the
                    # sender generation and already emitted the actual key-up.
                    # These assignments must never send input after Stop.
                    self._climb_state = ClimbState()
                    with self._direction_lock, self._hold_lock:
                        self._walk_hold_key = None
                        self._walk_hold_z = False
                        self._walk_hold_until = 0.0
                    if self.climbing_active_event is not None:
                        self.climbing_active_event.clear()
                    if self.dropping_active_event is not None:
                        self.dropping_active_event.clear()
                    if self.moving_active_event is not None:
                        self.moving_active_event.clear()
                    if self._patrol_state is not None:
                        self._patrol_state.write(False)
                    # Each (re)start of patrol gets a fresh jump-grace window.
                    self._patrol_started_at = None
                    continue
                if (self.automation_active_event is not None
                        and not self._automation_was_active):
                    # Input was just armed. ``enable_input`` already performed
                    # the one authoritative key scrub; repeating it here used
                    # to contend with the first attack press and visibly freeze
                    # a newly started patrol.
                    self._automation_was_active = True
                if self._patrol_started_at is None:
                    self._patrol_started_at = time.monotonic()
                # Attack priority: while the YOLO attack worker reports an
                # active target, hold patrol movement so the character stands
                # and fights - but only for a BOUNDED window.  Past
                # ``attack_block_max_seconds`` the patrol pushes through and
                # keeps walking (a stuck/unreachable target must not freeze
                # the patrol, e.g. after a monster knock-down).
                if self._attack_state is not None:
                    attack_active = self._attack_state.is_active()
                    if attack_active:
                        if self._attack_active_since is None:
                            self._attack_active_since = time.monotonic()
                        if (time.monotonic() - self._attack_active_since
                                > self.attack_block_max_seconds):
                            LOG.info(
                                "attack active %.1fs > %.1fs: patrol pushes "
                                "through",
                                time.monotonic() - self._attack_active_since,
                                self.attack_block_max_seconds,
                            )
                            attack_active = False
                    else:
                        self._attack_active_since = None
                    if attack_active:
                        # Mid-climb/drop: finish the climb.  The attack
                        # executor is already blocked by patrol_state busy,
                        # so releasing Up here would stop the character on
                        # the rope for nothing.
                        if self._attack_should_defer():
                            LOG.debug("attack active but climbing/dropping/stuck: "
                                      "finishing climb/jump")
                        else:
                            if not self._attack_paused_last:
                                LOG.info("attack active: patrol movement paused")
                                self._attack_paused_last = True
                            if self.climbing_active_event is not None:
                                self.climbing_active_event.clear()
                            if self.dropping_active_event is not None:
                                self.dropping_active_event.clear()
                            if self.moving_active_event is not None:
                                self.moving_active_event.clear()
                            self._release_climb_up()
                            self._release_walk_hold()
                            continue
                    if self._attack_paused_last:
                        LOG.info("attack clear: patrol movement resumed")
                        self._attack_paused_last = False
                minimap_region = self.minimap_region
                minimap_detection = None
                if self.minimap_detector is not None:
                    minimap_detection = self.minimap_detector.detect(frame.image)
                    minimap_region = minimap_detection.normalized_analysis_box(
                        frame.image.size
                    )
                    if minimap_detection.window_box != self._last_minimap_box:
                        width, height = minimap_detection.window_size
                        LOG.info(
                            "MINIMAP %s | box=%s | size=%dx%d | confidence=%.3f",
                            minimap_detection.source,
                            minimap_detection.window_box,
                            width,
                            height,
                            minimap_detection.confidence,
                        )
                        self._last_minimap_box = minimap_detection.window_box
                # Latest frame + region kept for the post-switch re-check of
                # other players on the new channel.
                self._last_frame = frame
                self._last_minimap_region = minimap_region
                observation = analyze_minimap(frame, minimap_region)
                # Raw (pre-pin) world-Y state of this frame, refreshed below
                # when the structure tracker runs; used by landing
                # reconciliation and the world-Y drift watchdog so they never
                # see the aliased/pinned value.
                self._raw_world_y = None
                self._raw_structure_confidence = 0.0
                if self.structure_tracker is not None and minimap_detection is not None:
                    analysis_rgb = np.asarray(
                        frame.image.crop(minimap_detection.analysis_box).convert("RGB")
                    )
                    structure_marker = detect_yellow_diamond(analysis_rgb)
                    tracking = self.structure_tracker.analyze(
                        frame, minimap_detection, structure_marker
                    )
                    self._raw_world_y = tracking.world_y_diamonds
                    self._raw_structure_confidence = tracking.confidence
                    observation = replace(
                        observation,
                        world_y_diamonds=tracking.world_y_diamonds,
                        structure_confidence=tracking.confidence,
                        scroll_y_diamonds=tracking.scroll_y_diamonds,
                    )
                    if tracking.mode != self._last_structure_mode:
                        LOG.info(
                            "MAP TRACKING mode=%s confidence=%.3f "
                            "scroll_y=%+.3f world_y=%s",
                            tracking.mode,
                            tracking.confidence,
                            tracking.scroll_y_diamonds,
                            (f"{tracking.world_y_diamonds:.3f}"
                             if tracking.world_y_diamonds is not None else "unknown"),
                        )
                        self._last_structure_mode = tracking.mode
                # Dispatched character position (per-frame, focus-independent
                # source): when the character worker is wired, its reading is
                # the authoritative marker - it overrides whatever this
                # worker detected on the same frame, so the position is
                # tracked every frame even while movement is paused /
                # suppressed (freeze fix: stale climb input / focus dips no
                # longer hide the real marker position).
                if self.character_positions is not None:
                    try:
                        dispatched = self.character_positions.get_nowait()
                    except queue.Empty:
                        dispatched = None
                    if _dispatched_position_matches(
                        dispatched, frame.sequence, minimap_region
                    ):
                        observation = replace(
                            observation,
                            player=Point(
                                float(dispatched.x), float(dispatched.y)
                            ),
                            confidence=dispatched.confidence,
                            marker_pixel_size=(
                                dispatched.marker_pixel_size
                                if dispatched.marker_pixel_size is not None
                                else observation.marker_pixel_size
                            ),
                        )
                        if self._dispatched_position_logged is None:
                            LOG.info(
                                "using dispatched character position "
                                "(x=%.6f y=%.6f confidence=%.2f)",
                                dispatched.x, dispatched.y, dispatched.confidence,
                            )
                            self._dispatched_position_logged = time.monotonic()
                coordinate_layout = None
                if (minimap_detection is not None
                        and observation.marker_pixel_size is not None
                        and observation.analysis_size is not None):
                    analysis_left, analysis_top, _, _ = minimap_detection.analysis_box
                    canvas_left, canvas_top, canvas_right, canvas_bottom = (
                        minimap_detection.canvas_box
                    )
                    marker_width, marker_height = observation.marker_pixel_size
                    if self.diamond_size_tracker is not None:
                        marker_width, marker_height = self.diamond_size_tracker.stabilize(
                            (marker_width, marker_height)
                        )
                    coordinate_layout = CoordinateLayout(
                        analysis_width=observation.analysis_size[0],
                        analysis_height=observation.analysis_size[1],
                        canvas_left=canvas_left - analysis_left,
                        canvas_top=canvas_top - analysis_top,
                        canvas_width=canvas_right - canvas_left,
                        canvas_height=canvas_bottom - canvas_top,
                        diamond_width=marker_width,
                        diamond_height=marker_height,
                    )
                self._current_horizontal_tolerance = self.horizontal_tolerance
                self._current_final_calculation_distance = (
                    self.final_calculation_distance
                )
                self._current_stair_jump_stall = STAIR_JUMP_STALL_FALLBACK
                if coordinate_layout is not None:
                    if self.horizontal_tolerance_diamonds is not None:
                        self._current_horizontal_tolerance = (
                            self.horizontal_tolerance_diamonds
                            * coordinate_layout.diamond_width
                            / coordinate_layout.analysis_width
                        )
                    if self.final_calculation_diamonds is not None:
                        self._current_final_calculation_distance = (
                            self.final_calculation_diamonds
                            * coordinate_layout.diamond_width
                            / coordinate_layout.analysis_width
                        )
                    # 卡住阈值固定 0.012（最小地图单位）：X 变化 < 0.012
                    # 连续 3 帧即判定卡住并跳。
                    self._current_stair_jump_stall = 0.012
                self._sync_patrol_controller(coordinate_layout)
                self._apply_pending_patrol_start(observation)
                self._consume_stair_jump_completion()
                # 自动重连: the first frame after the reconnect gave the input back decides
                # whether the character is on the patrol route, before the walk resumes.
                self._run_pending_route_check(observation, time.monotonic())
                # Cheap periodic sanity check over the marker already found
                # above. It can recover from a monster knock-down even when a
                # stale climb/drop phase would reject normal reconciliation.
                self._verify_out_of_range_floor(observation)
                # Reconcile route state with the actual marker Y before making
                # any movement decision. This handles falls from higher layers,
                # successful climbs, and external/manual layer changes alike.
                # Per-frame layer tracking, kept during the drop/return too:
                # the flicker guard keeps the current patrol layer while the
                # marker Y still sits inside its band, so an intermediate
                # platform cannot hijack the drop/return; a reading that
                # genuinely enters another route layer's band is followed
                # frame by frame.
                detected_layer_name = self._resync_route_layer(observation)
                self._log_detected_layer(detected_layer_name, observation)
                observation = self._pin_stationary_layer_world_y(observation)
                # Falling recovery: track rapid diamond-Y drops (an unexpected
                # fall - knocked down / missed a stair / walked off an edge).
                # Suppressed during the intentional drop-to-layer1, a return
                # drop/climb and active rope climbs, so those never get
                # interrupted.  Once a detected fall stops, the floor is
                # re-detected and patrol restarts there, or the character
                # returns to the patrol floor range.
                self._track_fall(observation)
                # World-Y drift watchdog: while cruising on a believed floor
                # the incremental tracker can drift over time; re-anchor
                # silently before the drift can poison layer recognition.
                self._world_drift_check(observation, time.monotonic())
                route_target_x, route_is_rope, route_label = self._route_target(observation)
                route_target_x = self._stabilize_rope_target(
                    route_target_x, route_is_rope, route_label
                )
                if (not self.stationary_attack_enabled
                        and self._advance_route_endpoint(observation, route_target_x)):
                    route_target_x, route_is_rope, route_label = self._route_target(observation)
                    route_target_x = self._stabilize_rope_target(
                        route_target_x, route_is_rope, route_label
                    )
                if (route_label != "drop-to-route"
                        and self._return_mode != "drop-to-route"):
                    # The landing evidence belongs to one descent only.  The
                    # mode check keeps it across a ``waiting-marker`` frame: one
                    # lost reading must not restart the measured descent.
                    self._reset_drop_arrival()
                if route_label == "drop-to-route":
                    # Return drop: keep dropping (Alt+Down) until the marker
                    # re-enters the patrol floor range, then restart patrol.
                    # The landing test is deliberately marker-only - the world-Y
                    # tracker is never accepted here, because at a fresh Start
                    # above the route it is anchored to the route's top layer
                    # and would otherwise end the drop before one Alt+Down was
                    # sent (see ``_drop_landing_floor``).
                    if self._return_mode == "drop-to-route":
                        self._note_drop_descent(observation)
                        floor = self._drop_landing_floor(observation)
                        if floor is not None:
                            self._finish_return(floor, observation)
                            route_target_x, route_is_rope, route_label = (
                                self._route_target(observation)
                            )
                            route_target_x = self._stabilize_rope_target(
                                route_target_x, route_is_rope, route_label
                            )
                elif route_label.endswith(".drop-to-first"):
                    if not self._descending_to_first:
                        # The planned descent owns every vertical move from here: a fall tracked before it
                        # must be forgotten, otherwise it survives the whole descent (``_track_fall`` only
                        # clears its frame counters while suppressed) and resolves right after the loop
                        # restart against a settle window that "settled" seconds ago - restarting the
                        # patrol on the floor the character came FROM instead of the first layer.
                        self._reset_fall_tracking()
                        # The descent owns the floors in between: this is where the resync guard (see
                        # _resync_route_layer) starts, and the per-floor log evidence is reset with it.
                        self._drop_descent_saw = None
                        LOG.info("final layer patrol done; descending to %s",
                                 self.first_layer)
                        self._descending_to_first = True
                        self._descending_since = time.monotonic()
                    elif (self._descending_since is not None
                          and time.monotonic() - self._descending_since
                          >= DROP_TO_FIRST_MAX_SECONDS):
                        # The descent owns the layer state, so a character that cannot drop any further
                        # must not sit here sending Alt+Down forever: hand the state back and let the
                        # resync follow the marker again.
                        LOG.warning(
                            "DROP TO FIRST: %.0fs without reaching %s; handing the layer state back to the "
                            "normal resync (the descent was not interrupted by a resync switch)",
                            DROP_TO_FIRST_MAX_SECONDS, self.first_layer,
                        )
                        self._descending_to_first = False
                        self._descending_since = None
                        self._drop_descent_saw = None
                    if self._final_drop_arrived(observation):
                        self._reset_route_loop()
                        self._reanchor_tracker_to_current_layer(observation)
                        route_target_x, route_is_rope, route_label = self._route_target(observation)
                elif (self.dropping_active_event is not None
                        and self.dropping_active_event.is_set()):
                    # Belt-and-braces: no drop in progress any more - the
                    # dropping flag must not linger (it blocks the attack).
                    self.dropping_active_event.clear()
                if self.near_rope_inner_range is not None:
                    rope_inner_distance = self.near_rope_inner_range
                elif coordinate_layout is not None and self.near_rope_diamonds is not None:
                    rope_inner_distance = (
                        self.near_rope_diamonds
                        * coordinate_layout.diamond_width
                        / coordinate_layout.analysis_width
                    )
                else:
                    rope_inner_distance = (
                        self.near_rope_range
                        if self.near_rope_range is not None
                        else self.estimated_final_speed * self.near_rope_seconds
                    )
                # The honey zone (tiny random step band) is the wider
                # near-range band; the inner band is the jump gate.
                rope_near_distance = (
                    self.near_rope_range
                    if self.near_rope_range is not None
                    else rope_inner_distance
                )
                inside_rope_zone = bool(
                    observation.player is not None
                    and route_is_rope
                    and route_target_x is not None
                    and abs(route_target_x - observation.player.x)
                    <= rope_inner_distance + 1e-9
                )
                if not inside_rope_zone and self._climb_state.failed_shift_used:
                    # A new approach may use one correction again. Staying in
                    # the zone cannot accumulate repeated Right holds.
                    self._climb_state = ClimbState()
                if not route_is_rope:
                    # Leaving the rope phase (next layer / new loop) starts a
                    # fresh FIRST approach: full continuous walk again.
                    self._rope_attempted = False
                # Every branch below may leave the target unset (stand-still,
                # waiting-marker, route-complete); initialize it so the log
                # line cannot hit an UnboundLocalError.
                active_target_x: Optional[float] = None
                # ``return.climb`` is a route-state label, not a saved layer
                # name.  Resolve this before branching so both normal patrol
                # and rope-climb jump checks use the same real layer.
                layer_for_jump = (
                    self._return_from_floor
                    if (self._return_mode == "climb-to-route"
                        and route_label == "return.climb")
                    else route_label.partition(".")[0]
                )
                if route_label == "patrol-paused":
                    if self.dropping_active_event is not None:
                        self.dropping_active_event.clear()
                    decision = MovementDecision(None, "patrol paused from UI")
                    active_target_x = None
                elif route_label == "route-complete":
                    decision = MovementDecision(None, "waiting for next layer calibration")
                elif route_label == "stationary-attack":
                    decision = self._stationary_attack_decision(observation)
                    active_target_x = self._stationary_attack_anchor.x if (
                        self._stationary_attack_anchor is not None
                    ) else None
                    # The pickup circuit is a real directional traversal of
                    # the anchor layer, not ordinary anchor recovery.  Give
                    # its current leg the same left/right jump-point lookup
                    # used by patrol, so a recorded 右跳 can replace the walk
                    # exactly when the pickup crosses it.
                    if (decision.reason.startswith("stationary pickup")
                            and self._stationary_route_anchor_layer):
                        pickup_jump = self._jump_point_decision(
                            observation,
                            self._stationary_route_anchor_layer,
                            None,
                            travel_direction=decision.key,
                        )
                        if pickup_jump is not None:
                            decision = pickup_jump
                    # Returning to the temporary stake is also a real
                    # horizontal traversal of its layer.  It can pass a
                    # left/right jump point before it reaches the final
                    # +/-0.02 anchor approach zone.  Previously only the
                    # optional pickup circuit consulted jump points here, so
                    # a layer-2 return walked straight through a valid point.
                    elif (decision.reason.startswith("stationary return walking")
                          and decision.key in ("left", "right")
                          and self._stationary_route_anchor_layer):
                        return_jump = self._jump_point_decision(
                            observation,
                            self._stationary_route_anchor_layer,
                            None,
                            travel_direction=decision.key,
                        )
                        if return_jump is not None:
                            decision = return_jump
                elif route_label == "stationary-attack-awaiting-anchor":
                    decision = MovementDecision(
                        None, "stationary attack requires a fresh Start Patrol position"
                    )
                elif route_label == "stand-still" or route_label.endswith(".stand-still"):
                    # No recorded action on the current layer (or nothing
                    # recorded at all): hold position; the attack worker
                    # (Fixed Attack / YOLO) keeps attacking.
                    decision = MovementDecision(
                        None, "no recorded patrol action; standing still"
                    )
                elif route_label == "waiting-marker":
                    decision = MovementDecision(
                        None, "waiting for the yellow marker"
                    )
                elif route_label == "return-climb-waiting":
                    # Return climb: the floor is momentarily unknown (marker
                    # Y between bands mid-climb) - hold, no keys.
                    decision = MovementDecision(
                        None, "return climb; waiting for the floor marker"
                    )
                elif (route_label.endswith(".drop-to-first")
                        or route_label == "drop-to-route"):
                    decision = MovementDecision(
                        "drop",
                        (f"final layer complete; repeat Alt+Down until {self.first_layer}"
                         if route_label.endswith(".drop-to-first")
                         else "outside patrol floor range; dropping until back in range"),
                        self.drop_chord_hold_seconds,
                    )
                    active_target_x = None
                    if route_label == "drop-to-route":
                        reconnect_recovery = self._reconnect_drop_recovery_decision(
                            observation, time.monotonic()
                        )
                        if reconnect_recovery is not None:
                            decision = reconnect_recovery
                elif route_is_rope and route_target_x is not None:
                    # JUMP-TO-ROPE vs MOVE-TO-ROPE: the minimap patrol zone
                    # (inside_rope_zone = the inner band) gates the JUMP.
                    # Outside the zone the character only WALKS (creep taps,
                    # never a jump).  Inside the zone the YOLO screen gap
                    # only refines the jump DIRECTION (straight up vs
                    # left/right) when fresh; it can never trigger a jump
                    # before the character reached the minimap jumping zone.
                    if inside_rope_zone:
                        # JUMP-TO-ROPE inside the minimap zone: the YOLO
                        # screen logic decides (straight up when right under
                        # the rope - tight gap or box overlap; left/right
                        # otherwise), with the minimap band jump as fallback
                        # when YOLO is stale.  In Fixed Attack mode the YOLO
                        # subprocess is not running: the minimap logic owns
                        # the jump (choose by current attack mode).
                        yolo_action = (
                            self._yolo_rope_action()
                            if self._yolo_detection_active else None
                        )
                        if yolo_action is not None:
                            decision = yolo_action
                        else:
                            rope_plan = move_towards_rope(
                                observation,
                                route_target_x,
                                rope_near_distance,
                                inner_range=rope_inner_distance,
                                under_rope_tolerance=self.under_rope_tolerance,
                                allow_climb=True,
                                horizontal_tolerance=self._current_horizontal_tolerance,
                                minimum_confidence=self.minimum_confidence,
                                movement_hold_seconds=self.movement_hold_seconds,
                                minimum_final_hold_seconds=self.minimum_final_hold_seconds,
                                minimum_movement_hold_seconds=self.minimum_movement_hold_seconds,
                                estimated_minimap_speed=self.estimated_minimap_speed,
                                final_calculation_distance=self._current_final_calculation_distance,
                                estimated_final_speed=self.estimated_final_speed,
                                final_move_safety_gain=self.final_move_safety_gain,
                                tiny_step_min_seconds=self.rope_tiny_step_min_seconds,
                                tiny_step_max_seconds=self.rope_tiny_step_max_seconds,
                            )
                            decision = rope_plan.decision
                        # Patrol-start grace: do not jump onto the rope the
                        # character happens to start next to - let it settle
                        # first, then resume the normal jump logic.
                        started = self._patrol_started_at
                        if (started is not None and decision.key
                                and decision.key.startswith("jump_climb_")
                                and time.monotonic()
                                < started + self.patrol_start_grace_seconds):
                            decision = MovementDecision(
                                None, "patrol start grace; no jump yet"
                            )
                        # A jump attempt has been made in this rope phase:
                        # any later re-approach is a RETRY (small steps).
                        self._rope_attempted = True
                        active_target_x = None
                    else:
                        if observation.player is not None:
                            live_gap = route_target_x - observation.player.x
                            if live_gap > 1e-9:
                                self._rope_approach_direction = "right"
                            elif live_gap < -1e-9:
                                self._rope_approach_direction = "left"
                        rope_state_fresh = (
                            self._yolo_detection_active
                            and self._rope_state is not None
                            and self._rope_state.is_fresh()
                        )
                        rope_plan = move_towards_rope(
                            observation,
                            route_target_x,
                            rope_near_distance,
                            inner_range=rope_inner_distance,
                            under_rope_tolerance=self.under_rope_tolerance,
                            allow_climb=not rope_state_fresh,
                            horizontal_tolerance=self._current_horizontal_tolerance,
                            minimum_confidence=self.minimum_confidence,
                            movement_hold_seconds=self.movement_hold_seconds,
                            minimum_final_hold_seconds=self.minimum_final_hold_seconds,
                            minimum_movement_hold_seconds=self.minimum_movement_hold_seconds,
                            estimated_minimap_speed=self.estimated_minimap_speed,
                            final_calculation_distance=self._current_final_calculation_distance,
                            estimated_final_speed=self.estimated_final_speed,
                            final_move_safety_gain=self.final_move_safety_gain,
                            tiny_step_min_seconds=self.rope_tiny_step_min_seconds,
                            tiny_step_max_seconds=self.rope_tiny_step_max_seconds,
                        )
                        decision = rope_plan.decision
                        active_target_x = rope_plan.target_x
                        if (rope_state_fresh and self._rope_attempted
                                and decision.key in ("left", "right")):
                            # RETRY approach (a jump was already attempted in
                            # this rope phase): short creep taps that re-check
                            # the screen gap every frame so the character
                            # walks into the jump window without overshooting
                            # the rope.  The FIRST approach keeps the plan's
                            # full tap (continuous walk like move-to-left-most
                            # / right-most).
                            creep = self.rope_approach_creep_seconds
                            gap = self._rope_state.screen_gap()
                            if gap is not None:
                                agap = abs(gap)
                                if agap <= self.rope_jump_px * 2.0:
                                    creep = min(creep, 0.12)
                                elif agap <= self.rope_jump_px * 4.0:
                                    creep = min(creep, 0.25)
                            decision = MovementDecision(
                                decision.key,
                                "MOVE TO ROPE (creep, retry)",
                                creep,
                            )
                else:
                    if route_target_x is None:
                        # A route transition can briefly leave the endpoint
                        # unresolved (for example while a recorded jump point
                        # hands its Up hold to rope climbing).  This is normal
                        # transient state, never an invariant failure.  Hold
                        # input for this frame and let the next fresh route
                        # calculation provide the endpoint.
                        decision = MovementDecision(
                            None, "patrol endpoint unresolved; waiting for fresh route target"
                        )
                        active_target_x = None
                    else:
                        target_y = observation.player.y if observation.player is not None else 0.0
                        position_target = Point(route_target_x, target_y)
                        if self._route_phase == "left":
                            position_plan = move_to_left_most(
                                observation,
                                position_target,
                                horizontal_tolerance=self._current_horizontal_tolerance,
                                movement_hold_seconds=self.movement_hold_seconds,
                                minimum_confidence=self.minimum_confidence,
                            )
                        else:
                            position_plan = move_to_right_most(
                                observation,
                                position_target,
                                horizontal_tolerance=self._current_horizontal_tolerance,
                                movement_hold_seconds=self.movement_hold_seconds,
                                minimum_confidence=self.minimum_confidence,
                            )
                        decision = position_plan.decision
                        jump_point_decision = self._jump_point_decision(
                            observation, layer_for_jump, position_plan
                        )
                        # Stairs that block the walk: when the marker stalls at a
                        # recorded jump-trigger X, replace the plain walk hold with
                        # a walk-and-jump (direction held, Alt tapped mid-hold).
                        phase_before_stair_check = self._route_phase
                        stair_decision = self._stair_jump_decision(
                            observation, route_label, position_plan, time.monotonic()
                        )
                        # Exhausting the stair budget can reroute this phase. The
                        # plan above belongs to the old direction, so do not send
                        # it after that reroute.
                        if self._route_phase != phase_before_stair_check:
                            decision = MovementDecision(
                                None, "boundary unreachable; waiting for rerouted patrol phase"
                            )
                            active_target_x = None
                        elif jump_point_decision is not None:
                            decision = jump_point_decision
                        elif stair_decision is not None:
                            decision = stair_decision
                        active_target_x = route_target_x
                if route_is_rope:
                    # At a rope, a planner may already be proposing an
                    # Alt+direction climb rather than a plain walk. Recover
                    # the actual horizontal travel direction from the rope
                    # gap so 右跳/左跳 retain their direction condition.
                    rope_travel_direction = (
                        decision.key if decision.key in ("left", "right") else None
                    )
                    if (rope_travel_direction is None
                            and observation.player is not None
                            and route_target_x is not None):
                        rope_gap = route_target_x - observation.player.x
                        if rope_gap > ROPE_JUMP_DIRECTION_DEAD_BAND:
                            rope_travel_direction = "right"
                        elif rope_gap < -ROPE_JUMP_DIRECTION_DEAD_BAND:
                            rope_travel_direction = "left"
                    # At the rope centre the gap is intentionally zero and
                    # the planner becomes a straight-up climb. The currently
                    # held walk is still the real direction by which the
                    # character entered this jump-point zone.
                    if (rope_travel_direction is None
                            and self._walk_hold_key in ("left", "right")):
                        rope_travel_direction = self._walk_hold_key
                    if rope_travel_direction is None:
                        rope_travel_direction = self._rope_approach_direction
                    if rope_travel_direction in ("left", "right"):
                        rope_jump_point = self._jump_point_decision(
                            observation,
                            layer_for_jump,
                            None,
                            travel_direction=rope_travel_direction,
                        )
                        if rope_jump_point is not None:
                            decision = rope_jump_point
                    climb_in_progress = bool(
                        self._climb_state.up_held
                        or decision.key in (
                            "climb", "jump_climb_left", "jump_climb_right", "jump_climb_up",
                        )
                        or (self.climbing_active_event is not None
                            and self.climbing_active_event.is_set())
                    )
                    if climb_in_progress:
                        # A rope transition can pass a jump point recorded
                        # either on the departing floor or the floor being
                        # reached.  In particular, a temporary stand-still
                        # return climbs from layer1 through a layer2 point;
                        # checking only layer1 made that point invisible even
                        # while the marker sat exactly on it.
                        climb_jump_layers: list[Optional[str]] = [
                            layer_for_jump
                        ]
                        if (self._return_mode == "climb-to-route"
                                and self.stationary_attack_enabled
                                and self._stationary_route_anchor_layer):
                            climb_jump_layers.append(
                                self._stationary_route_anchor_layer
                            )
                        elif (self._route_layer_index is not None
                              and self._route_layer_index + 1 < len(self._route_layers)):
                            climb_jump_layers.append(
                                self._route_layers[self._route_layer_index + 1]
                            )
                        climb_jump_point = None
                        seen_jump_layers: set[str] = set()
                        for jump_layer in climb_jump_layers:
                            if not jump_layer or jump_layer in seen_jump_layers:
                                continue
                            seen_jump_layers.add(jump_layer)
                            climb_jump_point = self._jump_point_decision(
                                observation,
                                jump_layer,
                                None,
                                climbing=True,
                                # A return climb has no ordinary left/right
                                # patrol decision, but it retains the actual
                                # side used to approach the rope. Preserve
                                # that direction so a matching 左跳/右跳 can
                                # fire while climbing back to the stake.
                                travel_direction=self._rope_approach_direction,
                            )
                            if climb_jump_point is not None:
                                break
                        if climb_jump_point is not None:
                            decision = climb_jump_point
                # A recorded 左跳/右跳 that is currently holding Up owns this
                # entire jump-to-rope attempt.  Do not let the ordinary rope
                # recovery reinterpret its tiny live X gap as an Alt+Left or
                # Alt+Right retry.  Those recovery chords are reserved for a
                # normal ``jump_climb_*`` (the built-in jump-to-rope target),
                # never for a directional jump-point record.
                if (route_is_rope and self._jump_point_up_held
                        # A new directional point is a deliberate replacement
                        # for the previous point's session.  It must reach the
                        # stair-jump worker, which releases the old Up claim
                        # before pressing the new direction + Alt chord.  The
                        # old broad guard turned this valid handoff into
                        # ``MOVE TO ROPE ... action=wait`` forever.
                        and decision.key not in (
                            "jump_point_left", "jump_point_right",
                        )):
                    self._yield_climb_to_jump_point()
                    decision = MovementDecision(
                        None,
                        "recorded directional jump point owns Up; waiting for landing",
                    )
                    active_target_x = None
                # Other-player safety net: a per-frame scan (no cooldown)
                # switches channel when other players appear.
                self._maybe_check_other_players(
                    time.monotonic(), frame, minimap_region
                )
                # Self-rescue: 5 分钟一检，角色连续 20 帧位置不变则
                # 回到第一层重启巡逻。
                self._rescue_stuck_check(observation, time.monotonic())
                self._update_jump_point_landing(observation)
                decision = preserve_persistent_climb(self._climb_state, decision)
                if route_label in ("route-complete", "patrol-paused"):
                    active_target_x = None
                climb_decision_active = decision.key in (
                    "climb", "jump_climb_left", "jump_climb_right",
                    "jump_climb_up", "drop", "stationary_jump",
                )
                anchor = self._stationary_attack_anchor
                player = observation.player
                anchor_layer = (
                    self._stationary_route_anchor_layer
                    or self._stationary_ui_anchor_layer
                )
                player_on_anchor_layer = bool(
                    anchor is not None
                    and player is not None
                    and (
                        (anchor_layer is not None
                         and self._layer_band_contains(anchor_layer, player.y))
                        or abs(anchor.y - player.y)
                        <= STATIONARY_ATTACK_SAME_LAYER_Y_TOLERANCE
                    )
                )
                player_in_stationary_attack_zone = bool(
                    player_on_anchor_layer
                    and player is not None
                    and abs(anchor.x - player.x)
                    <= STATIONARY_ATTACK_FINAL_APPROACH_X_RANGE
                )
                stationary_recovery_exclusive = bool(
                    self.stationary_attack_enabled
                    and anchor is not None
                    and (
                        # A route climb/drop is always exclusive, including
                        # its idle observations between physical key presses.
                        self._return_mode is not None
                        # Returning from another layer or walking toward the
                        # stake's final approach zone is not allowed to
                        # attack.  Only the +/-0.03 X zone may use the
                        # attack-bearing arbiter correction motions.
                        or not player_in_stationary_attack_zone
                        # Being inside the final zone is not enough: a
                        # pending anchor step or the opposite-side facing tap
                        # owns its own attack.  Hold the normal cadence until
                        # fresh marker reads confirm the standing position and
                        # requested facing, otherwise both paths race for the
                        # arbiter and can look like a character freeze.
                        or player is None
                        or not self._stationary_attack_ready_for_player(
                            anchor, player,
                        )
                        # A pickup leg remains exclusive even when the live
                        # decision has been replaced by its recorded jump
                        # point.  Do not let fixed attack interrupt that jump.
                        or self._stationary_pickup_phase is not None
                    )
                )
                # This independent event gates fixed attacks only while the
                # character is off-layer or returning to the 桩; its X
                # corrections carry their own attack now.
                if self.stationary_recovery_active_event is not None:
                    if stationary_recovery_exclusive:
                        self.stationary_recovery_active_event.set()
                    else:
                        self.stationary_recovery_active_event.clear()
                # Attack is blocked only while climb/drop input is active.
                # Once a new layer is confirmed and Up is released, attack
                # resumes immediately; the separate arrival timestamp still
                # suppresses unsafe stair jumps while the character settles.
                now_mono = time.monotonic()
                if (self.stationary_attack_enabled
                        and not self._stationary_return_route_ready):
                    # 站桩攻击 never climbs, drops, or returns to a route: a
                    # route/vertical state latched before or during the mode (a
                    # knock-down fall is the observed cause) must neither move
                    # the character nor block its attacks - the field log
                    # showed "attack skipped: climb/return input is active" on
                    # every beat while the character just stood there.
                    self._clear_stationary_route_state()
                    climbing_now = False
                else:
                    climbing_now = bool(
                        climb_decision_active
                        or self._climb_state.phase != "idle"
                        or self._player_switch_active
                        # Returning to the patrol floor range never attacks:
                        # the climb-back / drop-back is protected like a rope
                        # climb.
                        or self._return_mode is not None
                    )
                if self.climbing_active_event is not None:
                    if climbing_now:
                        self.climbing_active_event.set()
                    else:
                        self.climbing_active_event.clear()
                # Returning from another floor/far anchor position blocks
                # attack on purpose. Once both that exclusive recovery and
                # vertical input are finished, wake the attack cadence now
                # rather than waiting up to one complete configured interval.
                stationary_attack_blocked = bool(
                    self.stationary_attack_enabled
                    and (stationary_recovery_exclusive or climbing_now)
                )
                if stationary_attack_blocked:
                    self._stationary_attack_was_blocked = True
                elif self._stationary_attack_was_blocked:
                    self._stationary_attack_was_blocked = False
                    if self.stationary_attack_resume_event is not None:
                        self.stationary_attack_resume_event.set()
                    LOG.info(
                        "STATIONARY ATTACK: anchor and facing confirmed; "
                        "resuming normal attack cadence"
                    )
                # Publish the patrol state so the YOLO attack worker blocks
                # attacks during the active climbing operation: jump attempts,
                # retries, and the attached climb. Walking toward the rope and
                # confirmed-layer patrol keep attack priority.
                if self._patrol_state is not None:
                    busy_now = bool(
                        climbing_now
                        or (self.dropping_active_event is not None
                            and self.dropping_active_event.is_set())
                    )
                    # Hysteresis: once busy (climbing/dropping), stay busy for
                    # a grace window even through brief idle resets between
                    # climb attempts.  Without this, a stall reset wrote
                    # busy=false for one frame and the YOLO attack fired Ctrl
                    # exactly as the climb re-grabbed the rope, interrupting
                    # the climb.
                    if busy_now:
                        self._patrol_busy_until = now_mono + self._patrol_busy_hold
                    patrol_busy = bool(
                        busy_now or now_mono < self._patrol_busy_until
                    )
                    # Track the last horizontal direction the character was
                    # moved, so the attack worker can sync its facing belief
                    # (patrol walk taps also turn the character).  None-safe:
                    # no-op "wait" decisions during a climb must not crash the
                    # whole movement frame.
                    facing = self._patrol_facing_for_key(decision.key)
                    if facing is not None:
                        self._patrol_facing = facing
                    self._patrol_state.write(
                        patrol_busy, decision.key, self._patrol_facing
                    )
                # Walking state for the pickup worker: Z is only tapped while
                # the character is actually moving left/right (patrol walk or
                # rope approach), never while idle/aligned/climbing.
                self._update_moving_event(decision)
                if self.near_rope_event is not None:
                    if inside_rope_zone:
                        if not self.near_rope_event.is_set():
                            LOG.info("near rope: pausing Ctrl attack for final movement/climb")
                        self.near_rope_event.set()
                    else:
                        self.near_rope_event.clear()
                if decision.key == "climb":
                    decision = MovementDecision(
                        decision.key, decision.reason, self.climb_up_hold_seconds
                    )
                if decision.key == "aligned":
                    self._aligned_frames += 1
                    if self._aligned_frames >= self.aligned_frames_required:
                        decision = MovementDecision(
                            "climb",
                            f"saved rope X confirmed in {self._aligned_frames} fresh minimap frames",
                            self.climb_up_hold_seconds,
                        )
                    else:
                        decision = MovementDecision(
                            None,
                            f"saved rope X confirmation {self._aligned_frames}/"
                            f"{self.aligned_frames_required}",
                        )
                elif self._is_walk_key(decision.key):
                    self._aligned_frames = 0
                    self._release_climb_up()
                    self._climb_state = ClimbState()
                    self._climb_cycle_reset()
                if decision.key in ("left", "right"):
                    if route_label == "stationary-attack":
                        # Position correction must not be interrupted by a
                        # queued micro-step/buff movement action.
                        self._motion_arbiter_stage = None
                    elif route_is_rope and not inside_rope_zone:
                        self._motion_arbiter_stage = "move-to-rope"
                    elif not route_is_rope:
                        self._motion_arbiter_stage = "patrol"
                    else:
                        self._motion_arbiter_stage = None
                else:
                    self._motion_arbiter_stage = None
                self.last_observation, self.last_decision = observation, decision
                if observation.player is not None:
                    gap = ((active_target_x - observation.player.x)
                           if active_target_x is not None else None)
                    stage = ("CLIMB" if route_is_rope and inside_rope_zone else
                             "MOVE TO ROPE" if route_is_rope else "PATROL")
                    target_text = (f"{active_target_x:.6f}"
                                   if active_target_x is not None else "----")
                    gap_text = f"{gap:+.6f}" if gap is not None else "----"
                    LOG.info(
                        "%s| pos=(%.6f, %.6f) | target=%s | gap=%s | action=%s",
                        stage,
                        observation.player.x,
                        observation.player.y,
                        target_text,
                        gap_text,
                        decision.key or "wait",
                    )
                else:
                    LOG.warning("movement waiting: %s", decision.reason)
                now = time.monotonic()
                # 非行走决策（爬绳/跳跃/等待等）：先松开行走 hold，
                # 避免方向键/Z 残留。
                is_stair_jump = bool(
                    isinstance(decision.key, str)
                    and decision.key.startswith("stair_jump_")
                )
                is_jump_point = bool(
                    isinstance(decision.key, str)
                    and decision.key.startswith("jump_point_")
                )
                if decision.key not in ("left", "right") and not is_stair_jump and not is_jump_point:
                    self._release_walk_hold()
                # A recorded jump point is a narrow, one-frame positional
                # event.  It must not be lost merely because an ordinary walk
                # send occurred in the preceding 250 ms.
                if decision.key and (
                    is_jump_point
                    or now - self._last_send >= self.movement_cooldown
                ):
                    if decision.key in (
                        "drop", "climb", "jump_climb_left",
                        "jump_climb_right", "jump_climb_up",
                    ):
                        # Never send a climb/jump/drop key while the pickup
                        # worker still holds Z: even a few ms of Z+Up makes
                        # the game fire the skill and drop the rope.  Wait
                        # for the release (bounded, then force through).
                        if (self.pickup_active_event is not None
                                and self.pickup_active_event.is_set()
                                and now < self._pickup_z_force_after):
                            if self._pickup_z_force_after == 0.0:
                                self._pickup_z_force_after = now + 0.5
                            LOG.debug(
                                "climb/drop waiting for pickup Z release"
                            )
                            continue
                        self._pickup_z_force_after = 0.0
                    if decision.key == "drop":
                        if now - self._last_drop_attempt < self.drop_retry_seconds:
                            continue
                        if self.climbing_active_event is not None:
                            self.climbing_active_event.set()
                        if self.dropping_active_event is not None:
                            self.dropping_active_event.set()
                        if self.climb_attack_lock is None:
                            drop_sent = self._send_drop_through_platform()
                        else:
                            with self.climb_attack_lock:
                                drop_sent = self._send_drop_through_platform()
                        if not drop_sent:
                            if self.climbing_active_event is not None:
                                self.climbing_active_event.clear()
                            if self.dropping_active_event is not None:
                                self.dropping_active_event.clear()
                            continue
                        self._last_drop_attempt = now
                        self._note_reconnect_drop_attempt(observation, now)
                    elif decision.key in (
                        "climb", "jump_climb_left", "jump_climb_right",
                        "jump_climb_up",
                    ):
                        # Fresh attempts are rate-limited; an in-progress
                        # climb state machine advances every frame.
                        if self._climb_state.phase == "idle":
                            if now - self._last_climb_attempt < self.climb_attempt_interval_seconds:
                                continue
                            self._last_climb_attempt = now
                        if self._climb_state.phase == "idle":
                            # The rope can be recorded on a bench within the
                            # same logical layer. Pass the live marker X so
                            # the point-specific observed world-Y is used
                            # instead of the layer's flat fallback anchor.
                            self._reanchor_tracker_to_current_layer(observation)
                        # Direction comes from character X versus Rope X, with a
                        # dead band: an aligned character keeps the side its
                        # approach came from, and a character that already
                        # overshot the rope is never pushed further that way.
                        # The rope X here is the phase-locked target above, so
                        # projection jitter cannot flip the side mid-approach.
                        preferred_direction = (
                            self._climb_preferred_direction(
                                decision.key, observation, route_target_x
                            )
                            or self._rope_approach_direction
                            # climb() falls back to a left-first chord; keep the
                            # log honest about what will actually be pressed.
                            or "left"
                        )
                        self._log_climb_direction(
                            preferred_direction, observation, route_target_x
                        )
                        self._run_climb_step(
                            observation, route_target_x, preferred_direction
                        )
                    elif is_stair_jump or is_jump_point:
                        # The worker waits independently for the *current*
                        # fixed attack to end.  Keep walking in the patrol
                        # direction during that wait; do not release Left /
                        # Right and turn a confirmed stair recovery into a
                        # visible freeze.
                        direction = (decision.key.removeprefix("jump_point_")
                                     if is_jump_point else decision.key.removeprefix("stair_jump_"))
                        walking = self._send_walk_hold(MovementDecision(
                            direction,
                            "keep patrol walk while stair jump waits for attack",
                            self.movement_hold_seconds,
                        ))
                        request_stair_jump = getattr(
                            self.stair_jump_worker, "request", None
                        )
                        queued = False
                        if walking and callable(request_stair_jump):
                            if is_jump_point:
                                # Capture the active target at dispatch time,
                                # rather than guessing from the next frame's
                                # route label.  Only a point used to reach a
                                # rope may hand its Up hold to climb tracking.
                                self._jump_point_rope_handoff = bool(route_is_rope)
                            try:
                                queued = bool(request_stair_jump(
                                    direction,
                                    on_complete=self._on_stair_jump_complete,
                                    hold_up=is_jump_point,
                                ))
                            except TypeError:
                                queued = bool(request_stair_jump(direction))
                        if not queued:
                            if is_jump_point:
                                token = self._jump_point_candidate
                                if token is not None:
                                    # No input entered the worker, so retain no
                                    # false "inside" consumption; retry on the
                                    # next live frame while the marker remains
                                    # at the point.
                                    self._jump_point_inside_tokens.discard(token)
                                self._jump_point_candidate = None
                                self._jump_point_rope_handoff = False
                            self._on_stair_jump_complete(False)
                            LOG.warning("stair jump could not enter dedicated worker")
                        else:
                            if is_jump_point:
                                token = self._jump_point_candidate
                                if token is not None:
                                    self._jump_point_suppressed_tokens.add(token)
                                    # Once per leg (and once per pass for a
                                    # climb use): the same record must not jump
                                    # again on this leg or from the rope it just
                                    # grabbed.
                                    self._jump_point_note_fired(token)
                                    LOG.info(
                                        "JUMP POINT fired: %s[%d] is now used for "
                                        "leg pass=%d floor=%s phase=%s",
                                        token[0], token[1],
                                        self._jump_point_pass_id,
                                        self._jump_point_leg_key()[1],
                                        self._jump_point_leg_key()[2],
                                    )
                                self._jump_point_candidate = None
                            self._stair_jump_skip_frames = 5
                            LOG.info(
                                "%s registered; skipping the next 5 stair-detection frames",
                                "JUMP POINT" if is_jump_point else "STAIR JUMP",
                            )
                    elif decision.key in ("left", "right"):
                        # 陈旧爬绳输入刹车：决策是普通左右走，但爬绳状态仍认为
                        # Up 被按住（中途失败的抓绳尝试后焦点抖动松开了按键，状态机
                        # 却没收到松开通知），且标记 Y 仍落在当前巡逻楼层带内（确认
                        # 在地面而非绳弧上）时，先释放 Up 并重置爬绳状态，再继续行走。
                        if (observation.player is not None
                                and self._climb_state.up_held
                                and self._on_route_floor(observation.player.y)):
                            LOG.warning(
                                "stale climb input on floor walk: releasing "
                                "Up and resetting climb state"
                            )
                            self._release_climb_up()
                            self._climb_state = ClimbState()
                        # return.climb 也做绳上停滞恢复（与 .rope 同规则）：返回
                        # 爬绳向绳子的普通行走被平台边缘/挡板卡住时，停止按方向键，
                        # 改为朝绳跳/爬，避免无限循环。
                        # 绳上停滞恢复：角色实际已在绳上，或正被平台边缘挡在
                        # 绳旁边（X 不前进且与绳对齐）时，停止按方向键+Z，
                        # 改为爬绳 / 朝绳起跳，避免无限循环。
                        # 陈旧爬绳输入刹车：决策是普通左右走，但爬绳状态仍认为 Up 被按住
                        # （中途失败的抓绳尝试后焦点抖动松开了按键，状态机却没
                        # 收到松开通知——实测 layer1 上 pos=0.335106 冻结且
                        # 无任何按键发送）。标记 Y 仍落在当前巡逻楼层带内
                        # （确认站在地面而非绳弧上）时，先释放 Up 并重置爬绳
                        # 状态再继续行走；否则方向键会被爬绳输入门静默吞掉，
                        # 角色原地不动。
                        if (observation.player is not None
                                and self._climb_state.up_held
                                and self._on_route_floor(observation.player.y)):
                            LOG.warning(
                                "stale climb input on floor walk: releasing "
                                "Up and resetting climb state"
                            )
                            self._release_climb_up()
                            self._climb_state = ClimbState()
                        is_rope_approach = (
                            route_label.endswith(".rope")
                            or route_label == "return.climb"
                        )
                        if (is_rope_approach
                                and observation.player is not None
                                and self._rope_approach_stalled(
                                    observation.player.x,
                                    route_target_x,
                                    route_label,
                                )):
                            self._recover_rope_approach(
                                observation, route_target_x
                            )
                            continue
                        if (is_rope_approach
                                and self._rope_approach_far_stall_frames >= 6):
                            LOG.warning(
                                "ROPE approach walk frozen far from rope; "
                                "re-arming %s", decision.key
                            )
                            self._rope_approach_far_stall_frames = 0
                            self._rope_approach_far_stall_count += 1
                            # Stuck while a walk is issued: the game may be
                            # holding keys whose key-ups were lost (knock-down
                            # / focus dip).  Release every movement key once -
                            # the re-arm below re-presses what is needed.
                            self._release_stuck_keys()
                            self._release_walk_hold()
                            if self._rope_approach_far_stall_count >= 2:
                                # Still frozen after a full key release and a
                                # re-arm: a transient game lock (knock
                                # animation / monster push / stuck input
                                # state) may be blocking the walk - one
                                # recovery jump in place, then re-approach.
                                self._rope_approach_far_stall_count = 0
                                LOG.warning(
                                    "ROPE approach frozen after key release; "
                                    "recovery jump"
                                )
                                press = getattr(
                                    self.key_sender, "press", None
                                )
                                if press is not None:
                                    press(
                                        "alt",
                                        duration=getattr(
                                            self, "climb_nudge_seconds", 0.08
                                        ),
                                    )
                        # Cancellable walk hold: the movement key is released
                        # within ~20ms when the attack selects a target, so
                        # the character can face and hit a monster behind it.
                        self._send_walk_hold(decision)
                    elif decision.key == "stationary_jump":
                        # This is a decision label, not a WindowKeySender key.
                        # The actual input is Maple's Alt jump key.
                        _send_tap(
                            self.key_sender,
                            MovementDecision("alt", decision.reason, decision.duration),
                        )
                    else:
                        _send_tap(self.key_sender, decision)
                    self._last_send = now
                elif (decision.key is None
                        and self._climb_state.phase != "idle"):
                    # No-op frame (attached on-rope) while the climb state
                    # machine is active: advance it anyway so the fell-back /
                    # stall detection releases Up and retries.  Without this,
                    # a failed grab froze the character holding Up forever
                    # (the attached no-op never entered the send block).
                    self._run_climb_step(
                        observation, route_target_x, self._rope_approach_direction
                    )
            except Exception:
                # A bad frame must not kill the safety/control thread.
                LOG.exception("movement analysis failed; no key sent")
            finally:
                try:
                    self.frame_queue.task_done()
                except (AttributeError, ValueError):
                    pass
        self._release_climb_up()
        self._release_walk_hold()
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        LOG.info("movement worker stopped")


__all__ = [
    "DEFAULT_MINIMAP_REGION",
    "STAIR_JUMP_STALL_FALLBACK",
    "MinimapObservation",
    "MovementDecision",
    "MovementWorker",
    "RopeMovementPlan",
    "PositionMovementPlan",
    "ClimbState",
    "Point",
    "analyze_minimap",
    "climb",
    "detect_marker",
    "detect_layer_by_y",
    "detect_layer_by_world_y",
    "move_towards_rope",
    "move_to_left_most",
    "move_to_right_most",
    "plan_movement",
    "preserve_persistent_climb",
]
