"""Minimap-driven movement worker.

The worker consumes screenshots produced by ``capture_worker``.  It finds the
yellow player marker in the top-left minimap, estimates a nearby connector to
an upper platform, and emits short, conservative key taps through the shared
key sender.  It contains no global keyboard hooks and never sends keys itself;
the sender remains responsible for checking/focusing the configured window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import queue
import threading
import time
from typing import Any, Iterable, Optional, Protocol

import numpy as np
from PIL import Image

from marker_detector import DiamondSizeTracker, detect_yellow_diamond
from patrol_control import CoordinateLayout


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
    aligned_direction: Optional[str] = None,
    **movement_options: Any,
) -> RopeMovementPlan:
    """Stop at the nearest near-zone edge, then jump inward toward the rope."""

    player = observation.player
    if player is None:
        return RopeMovementPlan(
            "detect", None, rope_x, None,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )
    left_edge = rope_x - near_range
    right_edge = rope_x + near_range
    rope_gap = rope_x - player.x
    minimum_confidence = float(movement_options.get("minimum_confidence", 0.55))
    if observation.confidence < minimum_confidence:
        return RopeMovementPlan(
            "detect", player, rope_x, rope_gap,
            MovementDecision(None, "yellow marker missing or uncertain"),
        )

    if player.x < left_edge - 1e-9:
        edge, direction = left_edge, "right"
    elif player.x > right_edge + 1e-9:
        edge, direction = right_edge, "left"
    else:
        direction = "right" if rope_gap > 1e-9 else "left" if rope_gap < -1e-9 else aligned_direction
        if direction not in ("left", "right"):
            direction = "right"
        edge = left_edge if direction == "right" else right_edge
        return RopeMovementPlan(
            "climb", player, edge, edge - player.x,
            MovementDecision(
                f"jump_climb_{direction}",
                f"inside near zone; jump {direction} inward from edge",
                float(movement_options.get("minimum_final_hold_seconds", 0.08)),
            ),
        )

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
            f"move to {direction}-side near-zone edge {edge:.6f}; {hold_detail}",
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
    target_layer_frames: int = 0
    target_layer_since: Optional[float] = None
    last_world_y: Optional[float] = None
    stalled_frames: int = 0


def preserve_persistent_climb(
    state: ClimbState,
    proposed: MovementDecision,
) -> MovementDecision:
    """Never let a horizontal recalculation cancel an attached rope climb."""

    if state.phase == "climbing-up" and state.up_held:
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
) -> str:
    """Try to grab the rope and verify it from the next minimap screenshot.

    There is no centered Alt+Up attempt. The first attempt is always a
    simultaneous directional jump toward the rope. Screenshots between
    attempts verify upward Y; failure then tries the opposite direction.
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
        direction = preferred_direction if preferred_direction in ("left", "right") else "left"
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

    if persistent_up and state.up_held and state.phase == "climbing-up":
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
        return "climbing-up"

    if persistent_up and state.up_held:
        state.progress_check_frames += 1
        if (state.baseline_world_y is not None
                and observation.world_y_diamonds is not None
                and observation.structure_confidence >= 0.12):
            upward_progress = (
                state.baseline_world_y - observation.world_y_diamonds
            )
            attached = upward_progress >= world_y_change_required
            progress_detail = f"world Y +{upward_progress:.3f} diamonds"
        else:
            upward_progress = baseline - player.y if baseline is not None else 0.0
            attached = upward_progress >= y_change_required
            progress_detail = f"screen Y +{upward_progress:.6f}"
        if attached:
            state.phase = "climbing-up"
            state.last_world_y = observation.world_y_diamonds
            state.stalled_frames = 0
            LOG.info("CLIMB attached: keeping Up held (%s)", progress_detail)
            return "climbing-up"
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

    if state.phase in ("check-right", "check-primary-right", "check-primary-left"):
        # Recalculate from the newest screenshot. Never blindly reverse the
        # prior jump: if the character remains left of the rope, retry Right;
        # if it is now right of the rope, retry Left.
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
            # The route approaches the rope from the right after completing
            # right-most, so an exactly aligned/coarsely quantized marker uses
            # a leftward chord rather than the removed centered jump.
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
        climb_layer_confirm_seconds: float = 0.75,
        climb_nudge_seconds: float = 0.10,
        climb_y_change_required: float = 0.015,
        climb_world_y_change_required: float = 0.75,
        climb_world_y_stall_change_required: float = 0.15,
        climb_world_y_stall_frames: int = 2,
        climb_failed_shift_right_seconds: float = 0.01,
        near_rope_seconds: float = 0.5,
        near_rope_range: Optional[float] = None,
        near_rope_diamonds: Optional[float] = None,
        climb_attack_lock: Optional[threading.Lock] = None,
        climbing_active_event: Optional[threading.Event] = None,
        near_rope_event: Optional[threading.Event] = None,
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
        self.near_rope_seconds = near_rope_seconds
        self.near_rope_range = near_rope_range
        self.near_rope_diamonds = (
            float(near_rope_diamonds) if near_rope_diamonds is not None else None
        )
        self.climb_attack_lock = climb_attack_lock
        self.climbing_active_event = climbing_active_event
        self.near_rope_event = near_rope_event
        self.important_positions = important_positions or {}
        complete_layers = {
            name for name, value in self.important_positions.items()
            if isinstance(value, dict)
            and "left_most_pos" in value and "right_most_pos" in value
        }
        if route_order is not None:
            self._route_layers = [name for name in route_order if name in complete_layers]
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

        climb_input_active = (
            self._climb_state.up_held
            or self._climb_state.phase == "climbing-up"
        )
        expected_next_index = self._route_layer_index + 1
        if climb_input_active and detected_index == expected_next_index:
            now = time.monotonic()
            if self._climb_state.target_layer_since is None:
                self._climb_state.target_layer_since = now
            self._climb_state.target_layer_frames += 1
            arrival_seconds = now - self._climb_state.target_layer_since
            if (self._climb_state.target_layer_frames < self.climb_layer_confirm_frames
                    or arrival_seconds < self.climb_layer_confirm_seconds):
                LOG.info(
                    "CLIMB arrival confirmation: %s %d/%d %.2f/%.2fs; keeping Up held",
                    detected_name,
                    self._climb_state.target_layer_frames,
                    self.climb_layer_confirm_frames,
                    arrival_seconds,
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
        self._last_drop_attempt = float("-inf")
        if self.climbing_active_event is not None:
            self.climbing_active_event.clear()
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
        if observation.player is None or self.first_layer is None:
            return False
        layer = self.important_positions.get(self.first_layer)
        if not isinstance(layer, dict) or "layer_y" not in layer:
            return False
        if (observation.world_y_diamonds is not None
                and "layer_world_y" in layer
                and observation.structure_confidence >= 0.12):
            return abs(
                observation.world_y_diamonds - float(layer["layer_world_y"])
            ) <= float(layer.get("world_y_tolerance", 0.75))
        return abs(observation.player.y - float(layer["layer_y"])) <= float(
            layer.get("y_tolerance", 0.020000)
        )

    def _reset_route_loop(self) -> None:
        self._route_layer_index = self._route_layers.index(self.first_layer)
        self._route_phase = "left"
        self._climb_state = ClimbState()
        self._last_drop_attempt = float("-inf")
        LOG.info("returned to %s; starting new patrol loop", self.first_layer)

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
                    continue
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
                self._resync_route_layer(observation)
                observation = self._pin_stationary_layer_world_y(observation)
                route_target_x, route_is_rope, route_label = self._route_target(observation)
                if self._advance_route_endpoint(observation, route_target_x):
                    route_target_x, route_is_rope, route_label = self._route_target(observation)
                if route_label.endswith(".drop-to-first") and self._on_first_layer(observation):
                    self._reset_route_loop()
                    route_target_x, route_is_rope, route_label = self._route_target(observation)
                if coordinate_layout is not None and self.near_rope_diamonds is not None:
                    rope_jump_distance = (
                        self.near_rope_diamonds
                        * coordinate_layout.diamond_width
                        / coordinate_layout.analysis_width
                    )
                else:
                    rope_jump_distance = (
                        self.near_rope_range
                        if self.near_rope_range is not None
                        else self.estimated_final_speed * self.near_rope_seconds
                    )
                inside_rope_zone = bool(
                    observation.player is not None
                    and route_is_rope
                    and route_target_x is not None
                    and abs(route_target_x - observation.player.x) <= rope_jump_distance
                )
                if not inside_rope_zone and self._climb_state.failed_shift_used:
                    # A new approach may use one correction again. Staying in
                    # the zone cannot accumulate repeated Right holds.
                    self._climb_state = ClimbState()
                if route_label == "patrol-paused":
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
                    if observation.player is not None:
                        live_gap = route_target_x - observation.player.x
                        if live_gap > 1e-9:
                            self._rope_approach_direction = "right"
                        elif live_gap < -1e-9:
                            self._rope_approach_direction = "left"
                    rope_plan = move_towards_rope(
                        observation,
                        route_target_x,
                        rope_jump_distance,
                        aligned_direction=self._rope_approach_direction,
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
                decision = preserve_persistent_climb(self._climb_state, decision)
                if route_label in ("route-complete", "patrol-paused"):
                    active_target_x = None
                climb_decision_active = decision.key in (
                    "climb", "jump_climb_left", "jump_climb_right", "drop"
                )
                if self.climbing_active_event is not None:
                    if climb_decision_active or self._climb_state.phase != "idle":
                        self.climbing_active_event.set()
                    else:
                        self.climbing_active_event.clear()
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
                self.last_observation, self.last_decision = observation, decision
                if observation.player is not None:
                    gap = ((active_target_x - observation.player.x)
                           if active_target_x is not None else None)
                    stage = ("CLIMB" if route_is_rope and gap is not None
                             and abs(gap) <= rope_jump_distance else
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
                    if decision.key == "drop":
                        if now - self._last_drop_attempt < self.drop_retry_seconds:
                            continue
                        self._last_drop_attempt = now
                        if self.climbing_active_event is not None:
                            self.climbing_active_event.set()
                        if self.climb_attack_lock is None:
                            _drop_through_platform(
                                self.key_sender, self.drop_chord_hold_seconds
                            )
                        else:
                            with self.climb_attack_lock:
                                _drop_through_platform(
                                    self.key_sender, self.drop_chord_hold_seconds
                                )
                    elif decision.key in ("climb", "jump_climb_left", "jump_climb_right"):
                        if now - self._last_climb_attempt < 2.0:
                            continue
                        self._last_climb_attempt = now
                        if self._climb_state.phase == "idle":
                            self._reanchor_tracker_to_current_layer()
                        # Direction comes from character X versus Rope X. At
                        # an exactly quantized X, retain the last observed side
                        # of approach instead of using a fixed right-first rule.
                        preferred_direction = self._rope_approach_direction
                        if observation.player is not None and route_target_x is not None:
                            live_gap = route_target_x - observation.player.x
                            if live_gap > 1e-9:
                                preferred_direction = "right"
                            elif live_gap < -1e-9:
                                preferred_direction = "left"
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
                        )
                        LOG.info("climb recovery state: %s", result)
                        if result == "succeeded" and self._route_layers:
                            self._advance_after_climb()
                    else:
                        _send_tap(self.key_sender, decision)
                    self._last_send = now
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
