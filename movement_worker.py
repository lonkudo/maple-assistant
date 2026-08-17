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
from patrol_control import CoordinateLayout

from combat_coordination import AttackStateFile, PatrolStateFile, RopeStateFile
from channel_switch import channel_switch_procedure


LOG = logging.getLogger(__name__)


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


def detect_layer_by_y(
    player_y: float,
    layers: dict[str, Any],
) -> Optional[str]:
    """Return the nearest explicitly calibrated layer within its tolerance."""

    candidates = []
    for name, layer in layers.items():
        if not isinstance(layer, dict) or "layer_y" not in layer:
            continue
        gap = abs(float(layer["layer_y"]) - player_y)
        tolerance = float(layer.get("y_tolerance", 0.020000))
        if gap <= tolerance:
            candidates.append((gap, name))
    return min(candidates)[1] if candidates else None


def detect_layer_by_world_y(
    world_y: float,
    layers: dict[str, Any],
) -> Optional[str]:
    """Return nearest layer using scroll-compensated map-structure Y."""

    candidates = []
    for name, layer in layers.items():
        if not isinstance(layer, dict) or "layer_world_y" not in layer:
            continue
        gap = abs(float(layer["layer_world_y"]) - float(world_y))
        tolerance = float(layer.get("world_y_tolerance", 0.75))
        if gap <= tolerance:
            candidates.append((gap, name))
    return min(candidates)[1] if candidates else None


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
    under_rope_tolerance: float = 0.01,
    allow_climb: bool = True,
    **movement_options: Any,
) -> RopeMovementPlan:
    """Move into the inner rope band, then jump inward from within it.

    Only the inner gap gates the climb attempt: the outer honey-zone band is
    removed.  Outside the inner band the character simply walks toward the
    band edge; there is no "back away" stage.

    When the character is right under the rope (|gap| <= under_rope_tolerance,
    default +-0.01 minimap units) the climb is a straight jump up
    (``jump_climb_up``) instead of a left/right chord - a directional jump
    from directly under the rope just pushes the character past it.

    ``allow_climb=False`` (used while the YOLO screen gap is fresh and owns
    the jump decision) makes the inside-band case a short creep walk toward
    the rope instead of a jump, so the minimap plan can never race the YOLO
    jump logic.
    """

    player = observation.player
    if player is None:
        return RopeMovementPlan(
            "detect", None, rope_x, None,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )
    near_range = max(0.0, float(near_range))
    inner_range = max(0.0, float(inner_range))
    # Without an explicit inner gap the approach band is the single gate.
    band = inner_range if inner_range > 0 else near_range
    left_inner = rope_x - band
    right_inner = rope_x + band
    rope_gap = rope_x - player.x
    absolute_gap = abs(rope_gap)
    minimum_confidence = float(movement_options.get("minimum_confidence", 0.55))
    if observation.confidence < minimum_confidence:
        return RopeMovementPlan(
            "detect", player, rope_x, rope_gap,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )

    if absolute_gap <= band + 1e-9:
        if not allow_climb:
            # Fresh YOLO owns the jump decision: the minimap plan must only
            # walk here, creeping toward the rope center so the character
            # enters the screen-gap jump window.  A minimap jump issued from
            # inside this band would race the YOLO jump (the two coordinate
            # systems disagree near the rope).
            direction = "right" if rope_gap > 1e-9 else "left"
            return RopeMovementPlan(
                "move-to-rope-edge", player, rope_x, rope_gap,
                MovementDecision(
                    direction,
                    f"inside band; creep {direction} into jump range",
                    float(movement_options.get("minimum_final_hold_seconds", 0.08)),
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
    if player.x < left_inner - 1e-9:
        edge, direction = left_inner, "right"
    else:
        edge, direction = right_inner, "left"

    # Outside the near-rope zone, keep using the full fixed movement hold.
    # Shorten the hold only in the final edge-calculation zone immediately
    # before the near-zone edge. Calculating every approach from its distance
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
    if state.phase in ("climbing-up", "arrival-compensation"):
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
    climb_attach_frames: int = 2,
    arrival_y: Optional[float] = None,
    arrival_tolerance: float = 0.02,
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

    if persistent_up and state.up_held and state.phase == "arrival-compensation":
        return "arrival-compensation"

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
        # jump degrades to a Right chord (matches the old aligned default).
        if preferred_direction in ("left", "right"):
            retry_direction = preferred_direction
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

    # Both directional attempts failed. Shift slightly right before restarting
    # the search on a later fresh screenshot.
    if state.failed_shift_used:
        state.phase = "idle"
        state.baseline_y = None
        state.baseline_world_y = None
        state.up_held = False
        state.progress_check_frames = 0
        state.last_world_y = None
        state.stalled_frames = 0
        LOG.warning("CLIMB failed again; right correction already used for this approach")
        return "failed-cycle-no-more-shift"
    shifted = perform([
        MovementDecision(
            "right",
            "one-time right correction after failed left/right climb cycle",
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
            "CLIMB not verified after both directions; shifted right %.3fs",
            failed_cycle_right_seconds,
        )
        return "failed-cycle-shifted-right"
    LOG.warning("CLIMB not verified and right correction was blocked")
    return "input-blocked"


# Broad top-left crop.  Keeping it normalized makes initial calibration work
# across common 16:9/16:10 client resolutions.
# Only the map drawing inside the top-left minimap panel.  The old broad crop
# included yellow monsters/items in the game world and could mistake those for
# the player diamond.
DEFAULT_MINIMAP_REGION = (0.0, 0.075, 0.12, 0.24)


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
    box = (int(x0 * width), int(y0 * height), int(x1 * width), int(y1 * height))
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
    under_rope_tolerance: float = 0.01,
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
        return (
            float(layer["layer_y"]),
            float(layer.get("y_tolerance", 0.02)),
        )

    def _run_climb_step(
        self,
        observation: MinimapObservation,
        route_target_x: Optional[float],
        preferred_direction: Optional[str],
    ) -> str:
        """Advance the persistent climb state machine one frame."""

        arrival_y, arrival_tolerance = self._next_layer_arrival_band()
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
            arrival_y=arrival_y,
            arrival_tolerance=arrival_tolerance,
        )
        LOG.info("climb recovery state: %s", result)
        if result == "succeeded" and self._route_layers:
            self._advance_after_climb()
            self._climb_cycle_reset()
        elif result == "failed-cycle-no-more-shift":
            # Both jump directions failed and the one-time correction is
            # used up: a stuck-under-the-rope character restarts the route
            # at left-most after a few cycles instead of jumping in place.
            self._climb_cycle_failed()
        return result

    def _send_walk_hold(self, decision: MovementDecision) -> bool:
        """Hold a walk key, releasing EARLY when the YOLO attack takes over.

        Patrol walk taps can run for up to ``movement_hold_seconds`` (2s); a
        blocking hold would keep the character walking while the attack tries
        to face a monster behind it.  This hold polls the attack state and
        releases the movement key within ~20ms of the attack selecting a
        target, so the attack logic can turn the character and attack; patrol
        resumes on the next frame after the target clears (the top-of-loop
        attack gate skips movement while the attack is active).
        """

        if decision.key not in ("left", "right"):
            return _send_tap(self.key_sender, decision)
        if not _sender_is_safe(self.key_sender):
            LOG.warning("movement suppressed: target window is not safely selected")
            return False
        key_down = getattr(self.key_sender, "key_down", None)
        key_up = getattr(self.key_sender, "key_up", None)
        if key_down is None or key_up is None:
            return _send_tap(self.key_sender, decision)
        claimed = key_down(decision.key) is not False
        if not claimed:
            return False
        deadline = time.monotonic() + max(0.01, float(decision.duration))
        try:
            while time.monotonic() < deadline:
                if self.stop_event.is_set():
                    break
                if self._attack_state is not None:
                    attack_now = self._attack_state.is_active()
                    if attack_now:
                        if self._attack_active_since is None:
                            self._attack_active_since = time.monotonic()
                        if (time.monotonic() - self._attack_active_since
                                <= self.attack_block_max_seconds):
                            LOG.info("walk key released early: attack took over")
                            break
                    else:
                        self._attack_active_since = None
                time.sleep(0.02)
            return True
        finally:
            key_up(decision.key)

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
        climb_layer_confirm_seconds: float = 0.5,
        climb_nudge_seconds: float = 0.10,
        climb_y_change_required: float = 0.015,
        climb_world_y_change_required: float = 0.75,
        climb_world_y_stall_change_required: float = 0.15,
        climb_world_y_stall_frames: int = 2,
        climb_failed_shift_right_seconds: float = 0.01,
        climb_attempt_interval_seconds: float = 1.0,
        climb_failed_cycles_reset: int = 3,
        near_rope_seconds: float = 0.5,
        near_rope_range: Optional[float] = None,
        near_rope_inner_range: Optional[float] = None,
        near_rope_diamonds: Optional[float] = None,
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
        yolo_detection_active: bool = True,
        other_player_check_enabled: bool = False,
        other_player_drug_taps: int = 3,
        other_player_drug_gap_seconds: float = 1.0,
        other_player_hp_threshold: float = 0.70,
        other_player_switch_max_attempts: int = 3,
        other_player_switch_settle_seconds: float = 1.0,
        status_state_path: Optional[str] = None,
        drug_settings_path: Optional[str] = None,
    ) -> None:
        super().__init__(name="movement-worker", daemon=True)
        self.frame_queue = frame_queue
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
        self._climb_failures = 0
        self.near_rope_seconds = near_rope_seconds
        self.near_rope_range = near_rope_range
        self.near_rope_inner_range = (
            float(near_rope_inner_range)
            if near_rope_inner_range is not None else None
        )
        self.near_rope_diamonds = (
            float(near_rope_diamonds) if near_rope_diamonds is not None else None
        )
        self.climb_attack_lock = climb_attack_lock
        self.climbing_active_event = climbing_active_event
        self.dropping_active_event = dropping_active_event
        self.near_rope_event = near_rope_event
        self.important_positions = important_positions or {}
        complete_layers = {
            name for name, value in self.important_positions.items()
            if isinstance(value, dict)
            and "left_most_pos" in value and "right_most_pos" in value
        }
        if route_order is not None:
            self._route_layers = [
                name for name in route_order if name in complete_layers
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
                complete_layers,
                key=lambda name: int("".join(filter(str.isdigit, name)) or 0),
            )
        self._route_layer_index: Optional[int] = None
        self._route_phase = "left"
        self.patrol_enabled = patrol_enabled
        self.climbing_enabled = climbing_enabled
        self.final_layer_action = final_layer_action
        self.first_layer = first_layer or (self._route_layers[0] if self._route_layers else None)
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
        # the minimap at the moment a move-to-left event finished, switch
        # channel automatically.  Switched live from the UI.
        self._other_player_check_enabled = bool(other_player_check_enabled)
        self._left_excursion_active = False
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
        self.drug_settings_path = str(drug_settings_path) if drug_settings_path else str(
            Path(__file__).resolve().parent / "drug_settings.json"
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
        self._last_minimap_box: Optional[tuple[int, int, int, int]] = None
        self._last_structure_mode: Optional[str] = None
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

    def _update_moving_event(self, decision: MovementDecision) -> None:
        """Drive the walking-state event used to gate Z pickup."""

        if self.moving_active_event is None:
            return
        if decision.key in ("left", "right"):
            if not self.moving_active_event.is_set():
                LOG.debug("moving: pickup Z enabled")
            self.moving_active_event.set()
        else:
            if self.moving_active_event.is_set():
                LOG.debug("not moving: pickup Z paused")
            self.moving_active_event.clear()

    def _attack_should_defer_to_climb(self) -> bool:
        """True when an active attack must wait for the rope climb to finish.

        While the climb state machine owns the Up key (``up_held`` - grab
        attempt or attached climb) the attack waits: releasing Up mid-grab
        or mid-climb makes the character fall off the rope.  Walking toward
        the rope and the brief pause between attempts do NOT defer - there
        the attack keeps priority, and the climb_attack_lock already
        prevents a Ctrl from interleaving with the jump chord itself.
        """

        return bool(
            self._climb_state.up_held
            or (self.dropping_active_event is not None
                and self.dropping_active_event.is_set())
        )

        return bool(
            (self._climb_state.phase in (
                "climbing-up", "arrival-compensation",
            ) and self._climb_state.up_held)
            or (self.dropping_active_event is not None
                and self.dropping_active_event.is_set())
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

    def _handle_left_excursion_end(
        self, frame: Any, minimap_region: Any, route_label: str
    ) -> None:
        """Scan ONCE for other players when a move-to-left just finished.

        The red-diamond scan only runs at the moment the move-to-left
        excursion ends (the route advances from ``.left-most`` to
        ``.right-most``) - one scan per cycle, never continuously, so the
        minimap pixel check costs nothing the rest of the time.  When red
        diamonds (other players) show up, the channel switch is triggered.
        """

        is_left_most = str(route_label).endswith(".left-most")
        if is_left_most:
            self._left_excursion_active = True
            return
        if not self._left_excursion_active:
            return
        self._left_excursion_active = False
        if (not self._other_player_check_enabled
                or not str(route_label).endswith(".right-most")):
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
        self._left_excursion_active = False
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
            data = json.loads(
                Path(self.drug_settings_path).read_text(encoding="utf-8")
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
        attached = self._climb_state.phase in (
            "climbing-up", "arrival-compensation"
        )
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

    def _resync_route_layer(self, observation: MinimapObservation) -> Optional[str]:
        """Switch patrol state when the marker is detected on another layer.

        Falling and failed climbs can invalidate the expected route layer.  We
        check every fresh minimap frame, but only switch after Y falls inside a
        calibrated layer tolerance; intermediate airborne positions are ignored.
        """

        if observation.player is None or not self._route_layers:
            return None
        climb_input_active = (
            self._climb_state.up_held
            or self._climb_state.phase in ("climbing-up", "arrival-compensation")
        )
        expected_next_index = (
            self._route_layer_index + 1
            if self._route_layer_index is not None else -1
        )
        compensating = (
            climb_input_active
            and self._climb_state.phase == "arrival-compensation"
            and self._climb_state.target_layer_since is not None
            and 0 <= expected_next_index < len(self._route_layers)
        )
        if compensating:
            elapsed = time.monotonic() - self._climb_state.target_layer_since
            expected_name = self._route_layers[expected_next_index]
            if elapsed < self.climb_layer_confirm_seconds:
                LOG.info(
                    "CLIMB arrival compensation: %s %.2f/%.2fs; keeping Up held",
                    expected_name,
                    elapsed,
                    self.climb_layer_confirm_seconds,
                )
                return self._route_layers[self._route_layer_index]
            # The next layer was already confirmed. Finish the fixed Up hold
            # even if the centered marker/scroll estimate flickers afterward.
            detected_name = expected_name
        else:
            if climb_input_active:
                # During a climb the arrival is accepted from EITHER signal:
                # the minimap marker Y (works when the world-Y tracker sticks
                # to the lower layer) or the world Y (works when the minimap
                # marker aliases on a scrolling map).  The HIGHER detected
                # layer wins - the climb moves up, so a lower-layer reading
                # is the stale/aliased one.
                layers = {
                    name: self.important_positions[name]
                    for name in self._route_layers
                }
                marker_name = (
                    detect_layer_by_y(observation.player.y, layers)
                    if observation.player is not None else None
                )
                world_name = self._detected_layer(observation)

                def _index(name: Optional[str]) -> int:
                    if name is None or name not in self._route_layers:
                        return -1
                    return self._route_layers.index(name)

                detected_name = (
                    marker_name if _index(marker_name) >= _index(world_name)
                    else world_name
                )
            else:
                detected_name = self._detected_layer(observation)
        if detected_name is None:
            if self._climb_state.up_held or self._climb_state.phase == "climbing-up":
                self._climb_state.target_layer_frames = 0
                self._climb_state.target_layer_since = None
            return None
        detected_index = self._route_layers.index(detected_name)
        if self._route_layer_index is None:
            self._route_layer_index = detected_index
            self._route_phase = "left"
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
            if not compensating and self.climb_layer_confirm_seconds > 0:
                self._climb_state.target_layer_since = time.monotonic()
                self._climb_state.phase = "arrival-compensation"
                LOG.info(
                    "CLIMB layer %s seems reached; starting %.2fs Up compensation",
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
        self._release_climb_up()
        self._route_layer_index = detected_index
        self._route_phase = "left"
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
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        if self.near_rope_event is not None:
            self.near_rope_event.clear()
        returned_to_first = (
            self.first_layer is not None
            and detected_name == self.first_layer
            and previous_name != self.first_layer
        )
        if returned_to_first and self.structure_tracker is not None:
            first_layer = self.important_positions.get(self.first_layer, {})
            anchor_world_y = (
                first_layer.get("layer_world_y")
                if isinstance(first_layer, dict) else None
            )
            reanchor = getattr(self.structure_tracker, "reanchor_world_y", None)
            start_session = getattr(self.structure_tracker, "start_session", None)
            if anchor_world_y is not None and callable(reanchor):
                reanchor(float(anchor_world_y))
                LOG.info(
                    "MAP LOOP reset world Y at %s=%.6f",
                    self.first_layer,
                    float(anchor_world_y),
                )
            elif anchor_world_y is not None and callable(start_session):
                start_session(float(anchor_world_y))
                LOG.info(
                    "MAP LOOP reset world Y at %s=%.6f",
                    self.first_layer,
                    float(anchor_world_y),
                )
        if was_climbing:
            LOG.info("CLIMB complete: detected %s at y=%.6f",
                     detected_name, observation.player.y)
        else:
            LOG.warning(
                "LAYER CHANGED: %s -> %s at y=%.6f; restarting %s patrol",
                previous_name, detected_name, observation.player.y, detected_name,
            )
        return detected_name

    def _route_target(self, observation: MinimapObservation) -> tuple[Optional[float], bool, str]:
        """Return target X, whether near-target means climb, and route label."""

        if not self.patrol_enabled:
            return None, False, "patrol-paused"
        if observation.player is None or not self._route_layers:
            return self.fixed_target_x, True, "rope"
        self._select_route_layer(observation)
        if self._route_layer_index is None or self._route_layer_index >= len(self._route_layers):
            return None, False, "route-complete"
        name = self._route_layers[self._route_layer_index]
        layer = self.important_positions[name]
        if self._route_phase == "left":
            return float(layer["left_most_pos"]["x"]), False, f"{name}.left-most"
        if self._route_phase == "right":
            return float(layer["right_most_pos"]["x"]), False, f"{name}.right-most"
        is_final = self._route_layer_index == len(self._route_layers) - 1
        if is_final and (
            self.final_layer_action == "repeat_patrol"
            or (self.final_layer_action == "drop_to_first_layer"
                and len(self._route_layers) == 1)
        ):
            # Recover safely if a previous run/profile left this state at rope.
            self._route_phase = "left"
            return float(layer["left_most_pos"]["x"]), False, f"{name}.left-most"
        if is_final and self.final_layer_action == "drop_to_first_layer":
            return None, False, f"{name}.drop-to-first"
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
        final_name = snapshot.route_order[-1] if snapshot.route_order else None
        new_route = []
        for name in snapshot.route_order:
            layer = snapshot.layers.get(name)
            if not isinstance(layer, dict):
                continue
            required = ("left_most_pos", "right_most_pos")
            if name != final_name:
                required += ("rope_pos",)
            if all(point in layer for point in required):
                new_route.append(name)
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
            LOG.info("patrol route updated from UI: %s",
                     " -> ".join(new_route) if new_route else "none")

    def _advance_route_endpoint(self, observation: MinimapObservation, target_x: Optional[float]) -> bool:
        if observation.player is None or target_x is None:
            return False
        if self._route_phase == "left":
            # Passing the line counts. We intentionally do not turn this into
            # an exact-position problem at the minimap's coarse resolution.
            if observation.player.x > target_x + self._current_horizontal_tolerance:
                return False
            self._route_phase = "right"
            LOG.info("route endpoint reached/crossed: left-most; next right-most")
            return True
        if self._route_phase == "right":
            if observation.player.x < target_x - self._current_horizontal_tolerance:
                return False
            is_final = (
                self._route_layer_index is not None
                and self._route_layer_index == len(self._route_layers) - 1
            )
            if is_final and (
                self.final_layer_action == "repeat_patrol"
                or (self.final_layer_action == "drop_to_first_layer"
                    and len(self._route_layers) == 1)
            ):
                self._route_phase = "left"
                LOG.info(
                    "route endpoint reached/crossed: right-most; "
                    "single-layer patrol repeats at left-most"
                )
            elif self.climbing_enabled:
                self._route_phase = "rope"
                LOG.info("route endpoint reached/crossed: right-most; next rope")
            else:
                LOG.info("route endpoint reached/crossed: right-most; climbing disabled")
            return True
        return False

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
        marker_arrived = observation.player.y >= float(
            layer["layer_y"]
        ) - float(layer.get("y_tolerance", 0.020000))
        if marker_arrived:
            return True
        # Fall back to the world-Y signal when it is tracked and confident.
        if (observation.world_y_diamonds is not None
                and "layer_world_y" in layer
                and observation.structure_confidence >= 0.12):
            return observation.world_y_diamonds >= float(
                layer["layer_world_y"]
            ) - float(layer.get("world_y_tolerance", 0.75))
        return False

    def _reset_route_loop(self) -> None:
        self._route_layer_index = self._route_layers.index(self.first_layer)
        self._route_phase = "left"
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

    def _climb_cycle_failed(self) -> bool:
        """Count consecutive failed climb cycles at the rope.

        After ``climb_failed_cycles_reset`` full failed cycles (both jump
        directions tried, correction already used) the route restarts at
        left-most: the character walks away from the rope and re-approaches
        it from the edge, where the directional jump grabs reliably.  Without
        this a character stuck under a rope the straight jump cannot reach
        would jump in place forever and never patrol.
        """

        self._climb_failures += 1
        if self._climb_failures < self.climb_failed_cycles_reset:
            return False
        self._climb_failures = 0
        self._route_phase = "left"
        self._climb_state = ClimbState()
        LOG.warning(
            "CLIMB failed %d cycles at the rope; restarting patrol from left-most",
            self.climb_failed_cycles_reset,
        )
        return True

    def _climb_cycle_reset(self) -> None:
        self._climb_failures = 0

    def _advance_after_climb(self) -> None:
        assert self._route_layer_index is not None
        self._route_layer_index += 1
        self._route_phase = "left"
        self._climb_state = ClimbState()
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
            return abs(
                observation.world_y_diamonds - float(layer["layer_world_y"])
            ) <= float(layer.get("world_y_tolerance", 0.75))
        layer_y = float(layer.get("layer_y", layer["left_most_pos"]["y"]))
        tolerance = float(layer.get("y_tolerance", 0.020000))
        return abs(observation.player.y - layer_y) <= tolerance

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
        canonical = self._current_layer_world_y()
        if canonical is None:
            return observation
        return replace(
            observation,
            world_y_diamonds=canonical,
            structure_confidence=max(observation.structure_confidence, 1.0),
        )

    def _reanchor_tracker_to_current_layer(self) -> None:
        canonical = self._current_layer_world_y()
        reanchor = getattr(self.structure_tracker, "reanchor_world_y", None)
        if canonical is not None and callable(reanchor):
            reanchor(canonical)

    def run(self) -> None:
        LOG.info("movement worker started (%s)", "DRY-RUN" if getattr(self.key_sender, "dry_run", True) else "LIVE")
        while not self.stop_event.is_set():
            try:
                frame = self.frame_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if (self.automation_active_event is not None
                        and not self.automation_active_event.is_set()):
                    self._release_climb_up()
                    if self.climbing_active_event is not None:
                        self.climbing_active_event.clear()
                    if self.dropping_active_event is not None:
                        self.dropping_active_event.clear()
                    if self.moving_active_event is not None:
                        self.moving_active_event.clear()
                    if self._patrol_state is not None:
                        self._patrol_state.write(False)
                    continue
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
                        if self._attack_should_defer_to_climb():
                            LOG.debug("attack active but climbing/dropping: "
                                      "finishing climb")
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
                self._sync_patrol_controller(coordinate_layout)
                # Reconcile route state with the actual marker Y before making
                # any movement decision. This handles falls from higher layers,
                # successful climbs, and external/manual layer changes alike.
                # While descending to the first layer, resync is suppressed:
                # intermediate platforms must not hijack the drop and restart
                # patrol on a middle layer.
                if not self._descending_to_first:
                    self._resync_route_layer(observation)
                observation = self._pin_stationary_layer_world_y(observation)
                route_target_x, route_is_rope, route_label = self._route_target(observation)
                if self._advance_route_endpoint(observation, route_target_x):
                    route_target_x, route_is_rope, route_label = self._route_target(observation)
                if route_label.endswith(".drop-to-first"):
                    if not self._descending_to_first:
                        LOG.info("final layer patrol done; descending to %s",
                                 self.first_layer)
                        self._descending_to_first = True
                    if self._on_first_layer(observation):
                        self._reset_route_loop()
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
                if route_label == "patrol-paused":
                    if self.dropping_active_event is not None:
                        self.dropping_active_event.clear()
                    decision = MovementDecision(None, "patrol paused from UI")
                    active_target_x = None
                elif route_label == "route-complete":
                    decision = MovementDecision(None, "waiting for next layer calibration")
                elif route_label.endswith(".drop-to-first"):
                    decision = MovementDecision(
                        "drop",
                        f"final layer complete; repeat Alt+Down until {self.first_layer}",
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
                                rope_inner_distance,
                                inner_range=rope_inner_distance,
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
                            )
                            decision = rope_plan.decision
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
                            rope_inner_distance,
                            inner_range=rope_inner_distance,
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
                    active_target_x = route_target_x
                # Other-player safety net: one scan when a move-to-left
                # excursion finishes, then switch channel on sighting.
                self._handle_left_excursion_end(
                    frame, minimap_region, route_label
                )
                decision = preserve_persistent_climb(self._climb_state, decision)
                if route_label in ("route-complete", "patrol-paused"):
                    active_target_x = None
                climb_decision_active = decision.key in (
                    "climb", "jump_climb_left", "jump_climb_right",
                    "jump_climb_up", "drop",
                )
                if self.climbing_active_event is not None:
                    if climb_decision_active or self._climb_state.phase != "idle":
                        self.climbing_active_event.set()
                    else:
                        self.climbing_active_event.clear()
                # Publish the patrol state so the YOLO attack worker blocks
                # attacks ONLY while the character is on the rope (attached
                # climb) or dropping.  Moving toward the rope and the jump
                # attempts themselves are normal combat movement: attack
                # keeps priority there, so a passing mob is engaged instead
                # of ignored while walking to the rope.
                if self._patrol_state is not None:
                    on_rope = bool(
                        self._climb_state.phase in (
                            "climbing-up", "arrival-compensation",
                        )
                        and self._climb_state.up_held
                    )
                    busy_now = bool(
                        on_rope
                        or (self.dropping_active_event is not None
                            and self.dropping_active_event.is_set())
                    )
                    # Hysteresis: once busy (climbing/dropping), stay busy for
                    # a grace window even through brief idle resets between
                    # climb attempts.  Without this, a stall reset wrote
                    # busy=false for one frame and the YOLO attack fired Ctrl
                    # exactly as the climb re-grabbed the rope, interrupting
                    # the climb.
                    now_mono = time.monotonic()
                    if busy_now:
                        self._patrol_busy_until = now_mono + self._patrol_busy_hold
                    patrol_busy = bool(
                        busy_now or now_mono < self._patrol_busy_until
                    )
                    # Track the last horizontal direction the character was
                    # moved, so the attack worker can sync its facing belief
                    # (patrol walk taps also turn the character).
                    if decision.key in ("left", "right"):
                        self._patrol_facing = decision.key
                    elif decision.key in ("jump_climb_left", "jump_climb_right"):
                        self._patrol_facing = decision.key.removeprefix(
                            "jump_climb_"
                        )
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
                elif decision.key in ("left", "right"):
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
                        "%-12s | pos=(%.6f, %.6f) | target=%s | gap=%s | action=%s",
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
                            self._reanchor_tracker_to_current_layer()
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
                    elif decision.key in ("left", "right"):
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
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
        if self.dropping_active_event is not None:
            self.dropping_active_event.clear()
        LOG.info("movement worker stopped")


__all__ = [
    "DEFAULT_MINIMAP_REGION",
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
