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
import threading
import time
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
from config_store import config_section_file


LOG = logging.getLogger(__name__)


# Stall recovery is only valid at the rope itself.  Keeping this threshold in
# one place prevents the stall detector and its recovery action from drifting
# apart and turning a long rope approach into repeated jump-climb attempts.
ROPE_STALL_ALIGNMENT_RANGE = 0.03


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


def _layer_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    """Layer band from its recorded point Ys.

    band = (uppermost point Y - tolerance, lowermost point Y).
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
    values = _layer_point_ys(layer)
    if not values and isinstance(layer, dict) and "layer_y" in layer:
        values = [float(layer["layer_y"])]
    if not values:
        return None
    # Existing recordings store y_tolerance=0.02. Use half of that value for
    # the upper arrival margin so adjacent/nearby floors cannot both claim a
    # marker that is visibly on the higher layer. The full recorded point
    # span still covers stair-shaped paths from highest Y through lowest Y.
    effective_tolerance = max(0.0, float(tolerance)) * 0.5
    return min(values) - effective_tolerance, max(values)


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


def _layer_y_candidates(player_y: float, layers: dict[str, Any]) -> list[str]:
    """All marker-Y matches ordered from nearest recorded layer center."""

    candidates: list[tuple[float, str]] = []
    for name, layer in layers.items():
        if not isinstance(layer, dict) or "layer_y" not in layer:
            continue
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None or not band[0] - 1e-9 <= player_y <= band[1] + 1e-9:
            continue
        reference_y = float(layer.get("layer_y", (band[0] + band[1]) / 2.0))
        candidates.append((abs(player_y - reference_y), name))
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
    up_held: bool = False
    progress_check_frames: int = 0
    attach_frames: int = 0
    recent_y: list[float] = field(default_factory=list)
    target_layer_frames: int = 0
    target_layer_since: Optional[float] = None
    last_world_y: Optional[float] = None
    stalled_frames: int = 0
    # Consecutive frames the marker sits inside the NEXT layer's arrival
    # band while holding Up (rope-top settle).  Bounds how long the
    # at-arrival stall suppression may hold Up before retrying.
    arrival_frames: int = 0


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
    """Press Alt+Left/Right as one overlapping chord, then hold Up."""

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
    try:
        # These two key-down events remain active concurrently for the entire
        # directional jump window. They are not separate press() operations.
        direction_claimed = key_down(direction) is not False
        if not direction_claimed:
            return False
        alt_claimed = key_down("alt") is not False
        if not alt_claimed:
            return False
        time.sleep(max(0.025, direction_hold))
    finally:
        if alt_claimed:
            key_up("alt")
        if direction_claimed:
            key_up(direction)
    # Grab immediately after the directional jump chord. Any gap here lets
    # the character pass the rope before Up becomes active.
    if persistent_up:
        up_ok = key_down("up")
    else:
        up_ok = press("up", duration=climb_duration)
    return up_ok is not False


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
    world_y_stall_frames: int = 2,
    action_lock: Optional[threading.Lock] = None,
    preferred_direction: Optional[str] = None,
    failed_cycle_right_seconds: float = 0.01,
    persistent_up: bool = False,
    rope_x: Optional[float] = None,
    rope_x_tolerance: float = 0.025,
    straight_up_tolerance: float = 0.008,
    climb_attach_frames: int = 2,
    arrival_y: Optional[float] = None,
    arrival_tolerance: float = 0.02,
    arrival_in_progress: bool = False,
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
        direction = "up" if inside_straight_up_zone else (
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
    if persistent_up and state.up_held:
        if observation.player is not None:
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
            x_aligned = x_gap <= rope_x_tolerance
        else:
            x_gap = None
            x_aligned = True
        marker_rising = (
            baseline is not None
            and baseline - player.y >= y_change_required
        )
        if (state.baseline_world_y is not None
                and observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            world_progress = (
                state.baseline_world_y - observation.world_y_diamonds
            )
            world_rising = world_progress >= world_y_change_required
            progress_detail = f"world Y +{world_progress:.3f} diamonds"
        else:
            world_progress = 0.0
            world_rising = False
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
            # First confirmation frame: keep Up held, verify again next frame.
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
        if (inside_straight_up_zone
                and (state.phase != "check-primary-up"
                     or state.failed_shift_used)):
            # Do not repeat the same lateral chord while X is not changing.
            # After the one allowed side retry/correction, recovery stays
            # vertical instead of walking the character off the platform.
            retry_direction = "up"
        elif preferred_direction in ("left", "right"):
            retry_direction = preferred_direction
        elif player is not None and rope_x is not None:
            retry_direction = "right" if rope_x - player.x > 0 else "left"
        else:
            retry_direction = (
                "left" if state.phase in ("check-right", "check-primary-right")
                else "right"
            )

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
            state.stalled_frames = 0
            return f"{retry_direction}-retry-toward-rope"
        return "input-blocked"

    # Both attempts failed. Shift slightly TOWARD the live rope before
    # restarting on a later screenshot; the old hard-coded Right correction
    # moved away from ropes positioned to the left.
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
        """(marker Y, tolerance) of the NEXT route layer, or (None, 0.02)."""

        if (self._route_layer_index is None
                or self._route_layer_index + 1 >= len(self._route_layers)):
            return None, 0.02
        layer = self.important_positions.get(
            self._route_layers[self._route_layer_index + 1], {}
        )
        if not isinstance(layer, dict) or "layer_y" not in layer:
            return None, 0.02
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None:
            return float(layer["layer_y"]), tolerance
        return (
            (band[0] + band[1]) / 2.0,
            (band[1] - band[0]) / 2.0,
        )

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
        )
        LOG.info("climb recovery state: %s", result)
        if result == "succeeded" and self._route_layers:
            self._climb_arrival_at = time.monotonic()
            self._climb_cycle_reset()
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
        return result

    def _rescue_stuck_check(
        self, observation: MinimapObservation, now: float
    ) -> None:
        """Self-rescue stuck detection (checked once per 5-minute window).

        Long patrols can wedge the character in a corner or on a rope with
        no position change.  Within each ``rescue_check_interval_seconds``
        window the run of consecutive unchanged minimap positions is tracked
        (2 minimap pixels tolerance absorbs marker jitter); if it ever
        reaches ``rescue_stuck_frames`` (default 20) the character is stuck:
        drop to layer1 and restart the patrol.  Frames where an attack is
        active are skipped - movement is intentionally paused then.

        A MISSING yellow marker is itself a stuck condition: with no marker
        the worker sends no keys, the phase cannot advance, and the character
        freezes wherever it stands (e.g. an in-game UI window covering the
        top-left minimap after a long session).  Missing-marker frames count
        toward the same stuck window instead of resetting it, and when the
        run reaches ``rescue_stuck_frames`` the rescue fires IMMEDIATELY
        (not waiting for the next 5-minute window) so a covered/missing
        minimap cannot freeze the patrol forever.

        A marker present but OFF every recorded layer band has a separate
        consecutive stationary run: the character may have fallen off, but a
        transient adaptive-band miss while X/Y is progressing must not inherit
        ordinary stuck frames and drop a valid patrol floor.  A stationary
        off-route run rescues immediately at ``rescue_stuck_frames``. Frames
        with an active climb/drop are skipped because the marker legitimately
        passes between layer bands while climbing.
        """
        if not self.patrol_enabled or not self._route_layers:
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
            # Marker lost while patrol is active: the character cannot move
            # purposefully at all (no keys are sent, the phase cannot
            # advance) - count it as stuck instead of resetting, and rescue
            # once the run is long enough.  The immediate trigger keeps the
            # 5-minute window from delaying the recovery.
            self._rescue_stuck_frames += 1
            self._rescue_max_stuck = max(
                self._rescue_max_stuck, self._rescue_stuck_frames
            )
            if self._rescue_stuck_frames >= self.rescue_stuck_frames:
                LOG.warning(
                    "SELF-RESCUE: yellow marker missing for %d frames; "
                    "dropping to layer1 and restarting patrol",
                    self._rescue_stuck_frames,
                )
                self._rescue_stuck_frames = 0
                self._rescue_max_stuck = 0
                self._trigger_rescue()
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
            isinstance(layer, dict) and "layer_y" in layer
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
        else:
            self._rescue_stuck_frames = 0
            # Keep a fixed anchor throughout a no-progress run. Comparing
            # only adjacent frames made legitimate slow walking (<0.02 per
            # frame) look stationary forever despite large total travel.
            self._rescue_last_pos = Point(pos.x, pos.y)

    def _trigger_rescue(self) -> None:
        """Start the self-rescue in a background thread (guarded once)."""
        if self._rescue_active:
            return
        self._rescue_active = True
        threading.Thread(target=self._run_rescue, name="self-rescue",
                         daemon=True).start()

    def _run_rescue(self) -> None:
        """Drop to layer1 and restart the patrol from a clean state."""
        try:
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(False)
            self._drop_to_first_layer()
            self._restart_patrol_from_first_layer()
        except Exception:
            LOG.exception("self-rescue failed")
        finally:
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(True)
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
        if moved or not aligned:
            self._rope_approach_stall_frames = 0
            return False
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
        state.attach_frames = 2  # already attached to the rope
        state.stalled_frames = 0
        state.arrival_frames = 0
        self.key_sender.key_down("up")
        if self.climbing_active_event is not None:
            self.climbing_active_event.set()
        self._rope_stuck_recoveries += 1
        LOG.warning("ROPE STUCK recovery #%d: character on the rope; "
                    "climbing up (Z paused)", self._rope_stuck_recoveries)

    def _movement_busy_now(self) -> bool:
        """True only while climbing or dropping is actively in progress.

        The post-arrival timestamp still suppresses unsafe stair jumps, but it
        must not suppress attacks after the new layer has been confirmed.
        """
        if (self.dropping_active_event is not None
                and self.dropping_active_event.is_set()):
            return True
        if self._climb_state.phase != "idle":
            return True
        return False

    def _release_walk_hold(self) -> None:
        """Release the currently held walk direction (and Z) if any."""
        key_up = getattr(self.key_sender, "key_up", None)
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
                    if release:
                        self._release_walk_hold()
            except Exception:
                LOG.exception("hold manager failed")

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

        if decision.key not in ("left", "right"):
            return _send_tap(self.key_sender, decision)
        if not _sender_is_safe(self.key_sender):
            LOG.warning("movement suppressed: target window is not safely selected")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        if key_down is None:
            return _send_tap(self.key_sender, decision)
        with self._hold_lock:
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
                # 换方向：先松开旧键再按新键。
                self._release_walk_hold()
                claimed = key_down(decision.key) is not False
                if not claimed:
                    LOG.info(
                        "walk key %s send blocked (window not foreground "
                        "or input disabled) - character will not move",
                        decision.key,
                    )
                    return False
                self._walk_hold_key = decision.key
            if not self._walk_hold_z:
                if key_down("z") is not False:
                    self._walk_hold_z = True
                    self._pickup_count += 1
                    if self.pickup_active_event is not None:
                        self.pickup_active_event.set()
                    LOG.info("pickup: Z held with %s (#%d)",
                             decision.key, self._pickup_count)
            self._walk_hold_until = time.monotonic() + max(
                0.01, float(decision.duration)
            )
        return True

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
        aligned_frames_required: int = 2,
        climb_layer_confirm_frames: int = 3,
        climb_layer_confirm_seconds: float = 0.3,
        climb_arrival_world_tolerance: float = 0.20,
        climb_nudge_seconds: float = 0.10,
        climb_y_change_required: float = 0.015,
        climb_world_y_change_required: float = 0.75,
        climb_world_y_stall_change_required: float = 0.15,
        climb_world_y_stall_frames: int = 2,
        climb_failed_shift_right_seconds: float = 0.01,
        climb_attempt_interval_seconds: float = 1.0,
        climb_failed_cycles_reset: int = 3,
        patrol_cycles_per_layer: int = 2,
        near_rope_seconds: float = 0.5,
        near_rope_range: Optional[float] = None,
        near_rope_inner_range: Optional[float] = None,
        near_rope_diamonds: Optional[float] = None,
        under_rope_tolerance: float = 0.008,
        climb_attack_lock: Optional[threading.Lock] = None,
        climbing_active_event: Optional[threading.Event] = None,
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
        fall_detect_frames: int = 3,
        fall_marker_y_gain: float = 0.015,
        drop_chord_hold_seconds: float = 0.10,
        drop_retry_seconds: float = 1.0,
        minimap_detector: Any = None,
        patrol_controller: Any = None,
        diamond_size_tracker: Optional[DiamondSizeTracker] = None,
        structure_tracker: Any = None,
        automation_active_event: Optional[threading.Event] = None,
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
        yolo_detection_active: bool = True,
        other_player_check_enabled: bool = False,
        other_player_check_interval_seconds: float = 60.0,
        rescue_check_interval_seconds: float = 300.0,
        rescue_stuck_frames: int = 20,
        other_player_drug_taps: int = 3,
        other_player_drug_gap_seconds: float = 1.0,
        other_player_hp_threshold: float = 0.70,
        other_player_switch_max_attempts: int = 3,
        other_player_switch_settle_seconds: float = 1.0,
        status_state_path: Optional[str] = None,
        drug_settings_path: Optional[str] = None,
        stair_jump_enabled: bool = True,
        stair_jump_stall_diamonds: float = 0.25,
        stair_jump_stall_frames: int = 10,
        patrol_start_grace_seconds: float = 3.0,
        stair_jump_attempts_max: int = 3,
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
        self.climbing_active_event = climbing_active_event
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
        self._rope_approach_phase_label: Optional[str] = None
        self._rope_stuck_recoveries = 0
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
        self._return_mode: Optional[str] = None  # "climb-to-route" | "drop-to-route"
        # Floor the return started from: while climbing back, a failed
        # grab falls the marker back to a Y between every recorded band;
        # keep targeting this floor's own rope instead of waiting forever.
        self._return_from_floor: Optional[str] = None
        # Stable in-range floor detected at the top of a return climb. Return
        # climbs use the normal frame confirmation and rope-top compensation
        # before patrol is allowed to resume.
        self._return_arrival_floor: Optional[str] = None
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
            "stall_frames": 0,     # consecutive no-progress frames near a stair
            # Anchor for cumulative progress. Comparing only adjacent frames
            # misclassified normal ~0.008/frame walking as frozen because the
            # old threshold was 0.012/frame.
            "last_x": None,
            "attempts": 0,         # jumps issued at the current blockage
            "grace_until": 0.0,    # stall detection suspended until this time
            "gave_up": False,      # attempts exhausted; stop jumping this phase
        }

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

        Runs only in the move-to-left-most / move-to-right-most phases.  When
        the marker stalls (X not advancing while a walk hold is being issued)
        for ``stair_jump_stall_frames`` consecutive frames, a stair blocks the
        walk: return ``stair_jump_<direction>`` so the sender holds the travel
        direction and taps Alt (jump) mid-hold to clear it.  No jump points
        need to be recorded - any impassable stair is jumped automatically.
        Jumps are rate-limited by a grace window and capped so a truly
        impassable wall cannot make the character hop in place forever.
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
        state = self._stair_state
        if state["phase_label"] != route_label:
            self._reset_stair_state()
            state = self._stair_state
            state["phase_label"] = route_label
        px = observation.player.x
        anchor_x = state["last_x"]
        moved = (
            anchor_x is not None
            and abs(px - anchor_x) >= self._current_stair_jump_stall
        )
        if moved:
            # Cumulative progress across several small frame-to-frame steps is
            # still real walking. Re-anchor only after enough total movement;
            # otherwise a steady sub-threshold walk eventually looks frozen.
            state["last_x"] = px
            state["stall_frames"] = 0
            state["attempts"] = 0
            state["gave_up"] = False
            return None
        if anchor_x is None:
            state["last_x"] = px
        if now < state["grace_until"] or state["gave_up"]:
            return None
        state["stall_frames"] += 1
        if state["stall_frames"] < self.stair_jump_stall_frames:
            return None
        if state["attempts"] >= self.stair_jump_attempts_max:
            if not state["gave_up"]:
                LOG.warning(
                    "STAIR JUMP gave up at x=%.6f (%s): %d attempts without "
                    "progress; the boundary may be unreachable",
                    px, route_label, state["attempts"],
                )
                state["gave_up"] = True
                # 边界不可达（墙角/越界目标）：角色已在实际可到达的边界处，
                # 强制完成当前相位进入下一相位，而不是无限按方向键+Z。
                self._force_advance_phase(px)
            return None
        state["attempts"] += 1
        state["stall_frames"] = 0
        state["grace_until"] = now + self.stair_jump_grace_seconds
        LOG.info(
            "STAIR JUMP %s at x=%.6f attempt=%d/%d",
            direction, px, state["attempts"], self.stair_jump_attempts_max,
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
        # 卡在坑/边缘（台阶跳正要发出或正在重试）：攻击先等一跳。
        return bool(
            self._stair_state.get("stall_frames", 0) >= 1
            or self._stair_state.get("attempts", 0) > 0
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

    def _maybe_check_other_players(
        self, now: float, frame: Any, minimap_region: Any
    ) -> None:
        """Per-frame other-player scan for the automatic channel switch.

        No cooldown: as long as red diamonds (other players) show up on the
        minimap the channel switch fires on the very next frame.
        ``_player_switch_active`` guards re-entry while a switch is running;
        once it finishes the next frame re-checks and switches again if
        players are still present ("只要有人就换线").
        """

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
        LOG.warning("OTHER PLAYER detected on the minimap (%d); "
                    "switching channel", count)
        threading.Thread(
            target=self._run_other_player_switch, daemon=True
        ).start()

    def _run_other_player_switch(self) -> None:
        """Background: switch until clean, drop to layer1, restart patrol.

        No cooldown: while the NEW channel still has other players (red
        diamonds) the switch repeats immediately, up to
        ``other_player_switch_max_attempts`` per trigger.  Before each
        switch an HP drug is eaten only when HP is below
        ``other_player_hp_threshold`` (70%).  Once a channel is clean the
        character is dropped down to layer1 (it can spawn on ANY layer of
        the new channel), then the patrol restarts from layer1.
        """

        try:
            # The patrol must not fight the menu navigation keys.
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(False)
            switched = False
            clean = False
            attempts = 0
            while attempts < self.other_player_switch_max_attempts:
                attempts += 1
                self._drug_if_hp_low()
                ok = channel_switch_procedure(
                    self.key_sender,
                    on_press=lambda key, sent: LOG.info(
                        "player-switch press %s ok=%s", key, sent
                    ),
                )
                if not ok:
                    LOG.warning("player channel switch blocked; aborting")
                    break
                switched = True
                # The new channel: still other players -> switch again.
                time.sleep(self.other_player_switch_settle_seconds)
                count = self._other_players_on_latest_frame()
                if count == 0:
                    clean = True
                    LOG.warning("player channel switch done (attempt %d); "
                                "new channel clean", attempts)
                    break
                LOG.warning("other players still present after switch "
                            "attempt %d (%d); switching again",
                            attempts, count)
            else:
                LOG.warning("player channel switch gave up after %d attempts",
                            self.other_player_switch_max_attempts)
            if switched and clean:
                # The character may not respawn at layer1: drop down to it
                # first, then restart the patrol from a clean layer1 state
                # (route + world-Y anchor reset).
                self._drop_to_first_layer()
                self._restart_patrol_from_first_layer()
        except Exception:
            LOG.exception("player channel switch failed")
        finally:
            if self.patrol_controller is not None:
                self.patrol_controller.set_enabled(True)
            self._player_switch_active = False

    def _drop_to_first_layer(self) -> None:
        """Drop through platforms until the character stands on layer1.

        After a channel change the character can spawn on ANY layer (not
        necessarily layer1).  Repeated Alt+Down chords fall through the
        platforms; the loop ends when the minimap marker reaches layer1's
        band (marker-Y check, robust against the stale world-Y anchor
        right after a channel change).  Capped so a stuck character cannot
        drop forever.
        """

        if (self.first_layer is None
                or self.first_layer not in self._route_layers):
            return
        if len(self._route_layers) <= 1:
            # Single-layer route: the character is already on the first
            # (and only) layer - there is no lower platform to drop
            # through.  Skipping keeps the self-rescue instant instead of
            # pressing Alt+Down up to 30 times against the ground.
            LOG.info("single-layer map: skipping drop to %s", self.first_layer)
            return
        max_attempts = 30
        for attempt in range(1, max_attempts + 1):
            obs = self.last_observation
            if obs is not None and self._on_first_layer(obs):
                LOG.info("channel-switch drop: reached %s (attempt %d)",
                         self.first_layer, attempt)
                return
            LOG.info("channel-switch drop: attempt %d/%d (Alt+Down)",
                     attempt, max_attempts)
            try:
                _drop_through_platform(
                    self.key_sender, self.drop_chord_hold_seconds
                )
            except Exception:
                LOG.warning("drop suppressed during channel-switch descent",
                            exc_info=True)
            time.sleep(self.drop_retry_seconds)
        LOG.warning("channel-switch drop: gave up after %d attempts",
                    max_attempts)

    def _restart_patrol_from_first_layer(self) -> None:
        """Reset the route + world-Y anchor to the first layer (post-switch)."""

        if (not self._route_layers or self.first_layer is None
                or self.first_layer not in self._route_layers):
            return
        self._reset_route_loop()
        layer = self.important_positions.get(self.first_layer, {})
        anchor_world_y = (
            layer.get("layer_world_y") if isinstance(layer, dict) else None
        )
        if anchor_world_y is not None and self.structure_tracker is not None:
            reanchor = getattr(self.structure_tracker, "reanchor_world_y", None)
            start_session = getattr(
                self.structure_tracker, "start_session", None
            )
            if callable(reanchor):
                reanchor(float(anchor_world_y))
                LOG.info("MAP LOOP reset world Y at %s=%.6f",
                         self.first_layer, float(anchor_world_y))
            elif callable(start_session):
                start_session(float(anchor_world_y))
        LOG.info("channel switch complete: patrol restarted from %s",
                 self.first_layer)

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
                return detect_layer_by_y(observation.player.y, legacy_layers)
            return None
        if observation.player is None:
            return None
        return detect_layer_by_y(observation.player.y, layers)

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
        if not isinstance(layer, dict) or "layer_y" not in layer:
            return False
        tolerance = float(layer.get("y_tolerance", 0.020000))
        band = _layer_y_band(layer, tolerance)
        if band is None:
            return False
        return bool(band[0] - 1e-9 <= y <= band[1] + 1e-9)

    def _on_route_floor(self, y: float) -> bool:
        """True when the marker Y sits inside the CURRENT route layer's band."""
        if (self._route_layer_index is None
                or not 0 <= self._route_layer_index < len(self._route_layers)):
            return False
        return self._layer_band_contains(
            self._route_layers[self._route_layer_index], y
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
            return None
        if self._return_mode is not None:
            # Return-to-route owns the route state until it explicitly hands
            # patrol back to an in-range floor in ``_finish_return``.  The
            # normal resync used to see the stale pre-fall route index here
            # (for example layer3) and turn the successful layer1 -> layer2
            # return into a generic "layer3 -> layer2" backward transition.
            # That reset raced the dedicated return cleanup and could leave
            # layer2 repeating instead of advancing to layer3.
            return self._detect_floor_all(observation)
        climb_input_active = (
            self._climb_state.up_held
            or self._climb_state.phase == "climbing-up"
        )
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
                and self._layer_band_contains(
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
            marker_is_unambiguous = len(marker_candidates_all) == 1
            if marker_is_unambiguous:
                # This may deliberately be outside the active patrol range;
                # the guard below returns None so fall/return recovery owns
                # the transition instead of indexing a non-route layer.
                detected_name = marker_candidates_all[0]
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
            if (not marker_is_unambiguous
                    and world_name is not None
                    and world_name != detected_name):
                detected_name = world_name
            elif (marker_is_unambiguous
                    and world_name is not None
                    and world_name != detected_name):
                LOG.info(
                    "LAYER signal disagreement: marker=%s world=%s "
                    "world_y=%.6f confidence=%.3f; marker wins",
                    marker_candidates_all[0], world_name,
                    observation.world_y_diamonds,
                    observation.structure_confidence,
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
            route_layers = {
                name: self.important_positions[name]
                for name in self._route_layers
            }
            world_authoritative = bool(
                observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12
                and any(
                    isinstance(layer, dict) and "layer_world_y" in layer
                    for layer in route_layers.values()
                )
            )
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
        if detected_name is None:
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
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
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
                self._climb_state.target_layer_frames = 0
                self._climb_state.target_layer_since = None
            return None
        detected_index = self._route_layers.index(detected_name)
        if self._route_layer_index is None:
            self._route_layer_index = detected_index
            self._route_phase = "left"
            self._route_patrol_cycle = 1
            LOG.info("route starting on %s: left-most -> right-most -> rope",
                     detected_name)
            return detected_name
        if detected_index == self._route_layer_index:
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
                "LAYER CHANGED: %s -> %s at y=%.6f; restarting %s patrol",
                previous_name, detected_name, observation.player.y, detected_name,
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

    def prepare_patrol_start(self, floor: str) -> None:
        """Queue the independently detected startup floor for this worker."""

        with self._patrol_start_lock:
            self._pending_patrol_start_floor = str(floor)

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
        if floor is None:
            return False

        self._release_climb_up()
        self._release_walk_hold()
        self._climb_state = ClimbState()
        self._descending_to_first = False
        self._return_mode = None
        self._return_from_floor = None
        self._return_arrival_floor = None
        self._fall_pending = False
        self._fall_frames = 0
        self._fall_last_y = None
        self._forced_phase_entry = None
        self._aligned_frames = 0
        self._rope_approach_direction = None
        self._rope_attempted = False
        self._route_patrol_cycle = 1
        self._last_drop_attempt = float("-inf")
        self._patrol_busy_until = 0.0
        self._reset_stair_state()
        for event in (
            self.climbing_active_event,
            self.dropping_active_event,
            self.near_rope_event,
            self.moving_active_event,
        ):
            if event is not None:
                event.clear()

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
        self._route_layer_index = self._route_layers.index(floor)
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
        self._reanchor_tracker_to_current_layer(observation)

    def _detect_floor_all(self, observation: MinimapObservation) -> Optional[str]:
        """Detect the floor over ALL recorded layers (not just the patrol
        range), so an out-of-range landing is recognized for the return."""
        layers = {
            name: layer for name, layer in self.important_positions.items()
            if isinstance(layer, dict) and "layer_y" in layer
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
            return detect_layer_by_world_y(
                observation.world_y_diamonds, world_layers
            )
        return None

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
        """

        checked_at = time.monotonic() if now is None else float(now)
        if (checked_at - self._last_floor_verify_at
                < self._floor_verify_interval_seconds):
            return False
        self._last_floor_verify_at = checked_at
        if observation.player is None:
            self._floor_verify_candidate = None
            self._floor_verify_frames = 0
            return False
        marker_layers = {
            name: layer for name, layer in self.important_positions.items()
            if isinstance(layer, dict) and "layer_y" in layer
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
        self._fall_frames = 0
        self._fall_last_y = None
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.near_rope_event is not None:
            self.near_rope_event.clear()
        self._maybe_begin_return_if_out_of_range(observation)
        return self._return_mode is not None

    def _finish_return(
        self,
        floor: str,
        observation: Optional[MinimapObservation] = None,
    ) -> None:
        """After a return climb/drop reaches ``floor``: in range or not?  An
        in-range floor restarts patrol there; an out-of-range floor keeps the
        return mode pointed at the next step."""
        self._return_from_floor = floor
        if floor in self._route_layers:
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
        if (state.target_layer_since is not None
                and self._return_arrival_floor in self._route_layers):
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

        if floor not in self._route_layers:
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

    def _resolve_fall(self, observation: MinimapObservation) -> bool:
        """Called when a fall stops: re-detect the floor and act.

        In-range floor restarts patrol there (a same-floor bounce - jump /
        small pit - is ignored and patrol continues); an out-of-range floor
        starts the return-to-route climb/drop.  Returns False while no floor
        can be identified yet (kept pending for the next frame).
        """
        floor = self._detect_floor_all(observation)
        if floor is None:
            return False
        self._fall_pending = False
        self._fall_last_y = None
        self._fall_frames = 0
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
            self._fall_frames += 1
            return
        # The fall stopped (marker Y no longer dropping fast).
        if self._fall_pending:
            self._resolve_fall(observation)
        elif self._fall_frames >= self._fall_detect_frames:
            self._fall_pending = True
            self._resolve_fall(observation)
        self._fall_frames = 0

    def _maybe_begin_return_if_out_of_range(
        self, observation: MinimapObservation
    ) -> None:
        """When the marker is present, idle and on a floor OUTSIDE the
        patrol range (e.g. patrol started there, or the character settled
        there after a knock-back), start the return-to-range immediately -
        don't wait for a fall event.  The return mode also blocks attacking
        for the whole return: it drives ``climbing_now`` ->
        ``climbing_active_event`` + ``patrol_state`` busy, which both the
        YOLO executor and the legacy timed attack worker honour.
        """
        if (self._return_mode is not None
                or self._descending_to_first
                or self._climb_state.phase != "idle"
                or self._climb_state.up_held
                or observation.player is None):
            return
        floor = self._detect_floor_all(observation)
        if floor is None or floor in self._route_layers:
            return
        number = _layer_number(floor)
        self._reanchor_tracker_to_layer(floor, observation)
        self._return_mode = (
            "climb-to-route" if number < self._patrol_range_min
            else "drop-to-route"
        )
        self._return_from_floor = floor
        self._return_arrival_floor = None
        LOG.warning(
            "OUT OF PATROL RANGE: on %s outside patrol range; returning %s "
            "without attacking",
            floor,
            "climbing back" if self._return_mode == "climb-to-route"
            else "dropping back",
        )

    def _route_target(self, observation: MinimapObservation) -> tuple[Optional[float], bool, str]:
        """Return target X, whether near-target means climb, and route label.

        Left/Rope/Right are independent actions: the layer patrols exactly
        the recorded subset (in left -> right -> rope order).  With nothing
        recorded the worker stands still (``stand-still``) and only attacks.
        """

        if not self.patrol_enabled:
            return None, False, "patrol-paused"
        if not self._route_layers:
            # Nothing recorded on any layer: stand still (Fixed Attack / YOLO
            # keep attacking) instead of the old fall-back-to-rope walk.
            return None, False, "stand-still"
        if observation.player is None:
            return None, False, "waiting-marker"
        if self._return_mode == "drop-to-route":
            return None, False, "drop-to-route"
        if self._return_mode == "climb-to-route":
            # Return climb: walk to and climb the CURRENT floor's own rope
            # (the floor is below the patrol range).  When the climb lands
            # on the next floor ``_run_climb_step`` re-detects and either
            # restarts patrol or keeps climbing.
            detected_floor = self._detect_floor_all(observation)
            if self._return_climb_arrival_ready(detected_floor):
                floor = self._return_arrival_floor
                assert floor is not None
                LOG.info(
                    "RETURN TO ROUTE: climb settled on %s; restarting patrol",
                    floor,
                )
                self._finish_return(floor, observation)
                return self._route_target(observation)
            floor = detected_floor
            if floor is None or floor in self._route_layers:
                # Failed grab: the marker settled between recorded bands.  Keep
                # retrying/holding the rope of the floor the return started
                # from. An in-range reading must first complete stable-frame
                # confirmation and the rope-top compensation window.
                floor = self._return_from_floor
                if floor is None:
                    return None, False, "return-climb-waiting"
            rope = self.important_positions.get(floor, {}).get("rope_pos", {})
            rope_x = (
                float(rope["x"])
                if isinstance(rope, dict) and "x" in rope
                else self.fixed_target_x
            )
            return rope_x, True, "return.climb"
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
        self.first_layer = snapshot.patrol_start_layer or (
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
        if observation.player is None or target_x is None:
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
        if self._route_phase == "left":
            # Passing the line counts. We intentionally do not turn this into
            # an exact-position problem at the minimap's coarse resolution.
            if observation.player.x > target_x + self._current_horizontal_tolerance:
                return False
        elif self._route_phase == "right":
            if observation.player.x < target_x - self._current_horizontal_tolerance:
                return False
        else:
            # Rope phase advances through the climb machinery, not here.
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

        The first layer is the bottom of the route, so arrival means the
        marker's Y is AT or BELOW the recorded band (dropping further is
        impossible - the character is standing on it).  Both signals are
        OR-ed: the marker-Y check (reliable - the character is visibly on
        the platform) and the world-Y check (can drift when the structure
        tracker lags).  A strict symmetric tolerance or a single world-Y
        path failed when the drop landed a few pixels below the recorded
        ``layer_y`` / the world-Y estimate was stale, leaving the bot
        pressing Alt+Down forever.
        """

        if observation.player is None or self.first_layer is None:
            return False
        layer = self.important_positions.get(self.first_layer)
        if not isinstance(layer, dict) or "layer_y" not in layer:
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
        marker_matches_upper_floor = any(
            name != self.first_layer
            and isinstance(other_layer, dict)
            and self._layer_band_contains(name, observation.player.y)
            for name, other_layer in self.important_positions.items()
        )
        if marker_arrived and not marker_matches_upper_floor:
            return True
        # Fall back to the world-Y signal when marker Y is ambiguous and the
        # structure tracker is available and confident.
        if (observation.world_y_diamonds is not None
                and "layer_world_y" in layer
                and observation.structure_confidence >= 0.12):
            world_tol = float(layer.get("world_y_tolerance", 0.75))
            world_band = _layer_world_y_band(layer, world_tol)
            if world_band is None:
                return False
            return observation.world_y_diamonds >= world_band[0] - 1e-9
        return False

    def _reset_route_loop(self) -> None:
        self._route_layer_index = self._route_layers.index(self.first_layer)
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._forced_phase_entry = None
        self._climb_state = ClimbState()
        self._last_drop_attempt = float("-inf")
        self._descending_to_first = False
        if self.dropping_active_event is not None:
            # The drop phase is OVER (layer1 reached, new loop starts): the
            # dropping flag must clear, otherwise the patrol reports busy
            # forever and the YOLO attack stays blocked ("attack blocked:
            # patrol climbing/dropping" -> the character never attacks).
            self.dropping_active_event.clear()
        LOG.info("returned to %s; starting new patrol loop", self.first_layer)

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
        self._route_phase = "left"
        self._route_patrol_cycle = 1
        self._climb_state = ClimbState()
        self._climb_restarts += 1
        LOG.warning(
            "CLIMB failed %d cycles at the rope; restarting patrol from left-most",
            self.climb_failed_cycles_reset,
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
            key_up = getattr(self.key_sender, "key_up", None)
            if key_up is not None:
                key_up("up")
            self._climb_state.up_held = False

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
        """Ignore OpenCV vertical aliases while no vertical action is active."""

        if self._climb_state.up_held or self._climb_state.phase != "idle":
            return observation
        floor = self._current_route_floor()
        layer = self.important_positions.get(floor, {}) if floor else {}
        canonical = _layer_world_anchor_at_x(
            layer,
            observation.player.x if observation.player is not None else None,
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
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if (self.automation_active_event is not None
                        and not self.automation_active_event.is_set()):
                    self._release_climb_up()
                    self._release_walk_hold()
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
                if self.structure_tracker is not None and minimap_detection is not None:
                    analysis_rgb = np.asarray(
                        frame.image.crop(minimap_detection.analysis_box).convert("RGB")
                    )
                    structure_marker = detect_yellow_diamond(analysis_rgb)
                    tracking = self.structure_tracker.analyze(
                        frame, minimap_detection, structure_marker
                    )
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
                # Out-of-range floor with no fall in progress: start the
                # return right away (no attacking during it).
                self._maybe_begin_return_if_out_of_range(observation)
                route_target_x, route_is_rope, route_label = self._route_target(observation)
                if self._advance_route_endpoint(observation, route_target_x):
                    route_target_x, route_is_rope, route_label = self._route_target(observation)
                if route_label == "drop-to-route":
                    # Return drop: keep dropping (Alt+Down) until the marker
                    # re-enters the patrol floor range, then restart patrol.
                    if self._return_mode == "drop-to-route":
                        floor = self._detect_floor_all(observation)
                        if floor is not None and self._in_patrol_range(floor):
                            self._finish_return(floor, observation)
                            route_target_x, route_is_rope, route_label = (
                                self._route_target(observation)
                            )
                elif route_label.endswith(".drop-to-first"):
                    if not self._descending_to_first:
                        LOG.info("final layer patrol done; descending to %s",
                                 self.first_layer)
                        self._descending_to_first = True
                    if self._on_first_layer(observation):
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
                if route_label == "patrol-paused":
                    if self.dropping_active_event is not None:
                        self.dropping_active_event.clear()
                    decision = MovementDecision(None, "patrol paused from UI")
                    active_target_x = None
                elif route_label == "route-complete":
                    decision = MovementDecision(None, "waiting for next layer calibration")
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
                    assert route_target_x is not None
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
                    elif stair_decision is not None:
                        decision = stair_decision
                    active_target_x = route_target_x
                # Other-player safety net: a per-frame scan (no cooldown)
                # switches channel when other players appear.
                self._maybe_check_other_players(
                    time.monotonic(), frame, minimap_region
                )
                # Self-rescue: 5 分钟一检，角色连续 20 帧位置不变则
                # 回到第一层重启巡逻。
                self._rescue_stuck_check(observation, time.monotonic())
                decision = preserve_persistent_climb(self._climb_state, decision)
                if route_label in ("route-complete", "patrol-paused"):
                    active_target_x = None
                climb_decision_active = decision.key in (
                    "climb", "jump_climb_left", "jump_climb_right",
                    "jump_climb_up", "drop",
                )
                # Attack is blocked only while climb/drop input is active.
                # Once a new layer is confirmed and Up is released, attack
                # resumes immediately; the separate arrival timestamp still
                # suppresses unsafe stair jumps while the character settles.
                now_mono = time.monotonic()
                climbing_now = bool(
                    climb_decision_active
                    or self._climb_state.phase != "idle"
                    or self._player_switch_active
                    # Returning to the patrol floor range never attacks: the
                    # climb-back / drop-back is protected like a rope climb.
                    or self._return_mode is not None
                )
                if self.climbing_active_event is not None:
                    if climbing_now:
                        self.climbing_active_event.set()
                    else:
                        self.climbing_active_event.clear()
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
                if decision.key not in ("left", "right"):
                    self._release_walk_hold()
                if decision.key and now - self._last_send >= self.movement_cooldown:
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
                        self._last_drop_attempt = now
                        if self.climbing_active_event is not None:
                            self.climbing_active_event.set()
                        if self.dropping_active_event is not None:
                            self.dropping_active_event.set()
                        if self.climb_attack_lock is None:
                            _drop_through_platform(
                                self.key_sender, self.drop_chord_hold_seconds
                            )
                        else:
                            with self.climb_attack_lock:
                                _drop_through_platform(
                                    self.key_sender, self.drop_chord_hold_seconds
                                )
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
                        # Direction comes from character X versus Rope X. At
                        # an exactly quantized X, retain the last observed side
                        # of approach instead of using a fixed right-first rule.
                        preferred_direction = self._rope_approach_direction
                        if decision.key == "jump_climb_up":
                            # The YOLO/minimap plan decided the character is
                            # right under the rope: jump straight up.  Do not
                            # let the minimap live-gap fallback below override
                            # it with a left/right chord.
                            preferred_direction = "up"
                        elif observation.player is not None and route_target_x is not None:
                            live_gap = route_target_x - observation.player.x
                            if live_gap > 1e-9:
                                preferred_direction = "right"
                            elif live_gap < -1e-9:
                                preferred_direction = "left"
                        self._run_climb_step(
                            observation, route_target_x, preferred_direction
                        )
                    elif decision.key.startswith("stair_jump_"):
                        # Stuck at a recorded stair trigger: hold the travel
                        # direction and tap Alt (jump) mid-hold to clear it.
                        self._send_stair_jump(decision)
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
                        # Cancellable walk hold: the movement key is released
                        # within ~20ms when the attack selects a target, so
                        # the character can face and hit a monster behind it.
                        self._send_walk_hold(decision)
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
