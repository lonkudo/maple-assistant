"""Thread-safe patrol controls and persistent multi-layer calibration."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
from typing import Any, Literal, Optional
def _layer_point_ys(layer: Any) -> list[float]:
    values = []
    for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
        point = layer.get(point_name)
        if isinstance(point, dict) and "y" in point:
            values.append(float(point["y"]))
    return values


def _layer_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    values = _layer_point_ys(layer)
    if not values and isinstance(layer, dict) and "layer_y" in layer:
        values = [float(layer["layer_y"])]
    if not values:
        return None
    # band = (topmost point Y - tolerance, lowermost point Y): the tolerance
    # is applied only ABOVE the topmost point (where climbs/drops arrive),
    # not below the lowermost point, so the band does not reach into the
    # layer BELOW - adjacent floors' bands overlap less.
    return min(values) - tolerance, max(values)


def _layer_world_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    values = []
    for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
        point = layer.get(point_name)
        if isinstance(point, dict) and "world_y" in point:
            values.append(float(point["world_y"]))
    if not values and isinstance(layer, dict) and "layer_world_y" in layer:
        values = [float(layer["layer_world_y"])]
    if not values:
        return None
    # Same rule as _layer_y_band: tolerance only above the topmost point,
    # never below the lowermost point (no reach into the layer below).
    return min(values) - tolerance, max(values)




LOG = logging.getLogger(__name__)


PointKind = Literal["left_most_pos", "rope_pos", "right_most_pos"]
Boundary = Literal["left_most_pos", "right_most_pos"]


def _layer_number(name: str) -> int:
    """Trailing floor number of a layer name (``layer12`` -> 12)."""
    match = re.search(r"(\d+)$", name)
    return int(match.group(1)) if match else 0
REQUIRED_LAYER_POINTS: tuple[PointKind, ...] = (
    "left_most_pos", "rope_pos", "right_most_pos"
)
PATROL_EDGE_POINTS: tuple[PointKind, ...] = (
    "left_most_pos", "right_most_pos"
)

# Every point that constitutes an independent patrol action.  A layer patrols
# with any non-empty subset of these - record only the points you want (e.g. a
# rope-only layer climbs straight to its rope; an empty layer stands still and
# only attacks).  Kept as a tuple of PointKind for type compatibility.
ACTION_POINTS: tuple[PointKind, ...] = (
    "left_most_pos", "right_most_pos", "rope_pos"
)


def _layer_present_actions(layer: Any) -> list[str]:
    """Return the names of a layer's recorded action points (x/y present)."""

    if not isinstance(layer, dict):
        return []
    return [
        name for name in ACTION_POINTS
        if isinstance(layer.get(name), dict)
        and "x" in layer[name] and "y" in layer[name]
    ]


@dataclass(frozen=True)
class RecordedEndpoint:
    layer: str
    boundary: PointKind
    x: float
    y: float


@dataclass(frozen=True)
class PatrolSnapshot:
    enabled: bool
    selected_layer: str
    route_order: tuple[str, ...]
    layers: dict[str, Any]
    climbing_enabled: bool
    final_layer_action: str
    patrol_start_layer: str = ""
    patrol_end_layer: str = ""
    patrol_range_set: bool = False


@dataclass(frozen=True)
class CoordinateLayout:
    """Geometry needed to map points across minimap width and zoom changes."""

    analysis_width: float
    analysis_height: float
    canvas_left: float
    canvas_top: float
    canvas_width: float
    canvas_height: float
    diamond_width: float
    diamond_height: float

    def stable_point(self, x: float, y: float) -> tuple[float, float]:
        center_x = self.canvas_left + self.canvas_width / 2.0
        center_y = self.canvas_top + self.canvas_height / 2.0
        return (
            (x * self.analysis_width - center_x) / max(1.0, self.diamond_width),
            (y * self.analysis_height - center_y) / max(1.0, self.diamond_height),
        )

    def project(self, stable_x: float, stable_y: float) -> tuple[float, float]:
        center_x = self.canvas_left + self.canvas_width / 2.0
        center_y = self.canvas_top + self.canvas_height / 2.0
        return (
            (center_x + stable_x * self.diamond_width) / self.analysis_width,
            (center_y + stable_y * self.diamond_height) / self.analysis_height,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            name: round(float(getattr(self, name)), 6)
            for name in self.__dataclass_fields__
        }


class PatrolController:
    """Own settings shared by the UI and movement worker.

    The UI records one selected layer at a time. A newly added layer becomes an
    active patrol layer only after Left, Rope, and Right have all been recorded.
    """

    def __init__(self, profile_path: Path, profile: dict[str, Any]) -> None:
        self.profile_path = Path(profile_path)
        self._profile = deepcopy(profile)
        self._enabled = bool(profile.get("patrol_enabled", False))
        route = list(profile.get("route_order", []))
        layers = profile.get("layers", {})
        self._selected_layer = route[-1] if route else next(iter(layers), "layer1")
        # Contiguous patrol floor range lives in the profile
        # (``patrol_start_layer`` / ``patrol_end_layer``) so recordings, the
        # UI and the movement worker all see the same persisted selection.
        self._lock = threading.RLock()

    def _sorted_layer_names_locked(self) -> list[str]:
        """Bottom-up layer order by numeric suffix, never recording order.

        The recorded ``route_order`` follows the order points happened to be
        recorded in; recording the top layer before a lower one ("Add Layer"
        auto-selects the new layer) would otherwise put layer1 above layer2
        in the patrol route and the UI.
        """

        def _number(name: str) -> int:
            match = re.search(r"(\d+)$", name)
            return int(match.group(1)) if match else 0

        return sorted(
            self._profile.get("route_order", []), key=_number
        )

    def snapshot(self, layout: Optional[CoordinateLayout] = None) -> PatrolSnapshot:
        with self._lock:
            layers = deepcopy(self._profile.get("layers", {}))
            # A saved rope on the map-top layer is retained on disk so it can
            # become useful if another layer is added, but it is invisible to
            # current UI logic while that layer is the map top.  The patrol
            # range's own end-floor rope is omitted by the movement worker's
            # final-floor logic instead (the last patrolled floor never
            # climbs), so the UI keeps the old map-top semantics.
            final_layer = self._final_layer_name_locked()
            if final_layer is not None and isinstance(layers.get(final_layer), dict):
                layers[final_layer].pop("rope_pos", None)
            if layout is not None:
                self._project_layers_locked(layers, layout)
            return PatrolSnapshot(
                enabled=self._enabled,
                selected_layer=self._selected_layer,
                route_order=tuple(self._sorted_layer_names_locked()),
                layers=layers,
                climbing_enabled=bool(self._profile.get("climbing_enabled", True)),
                final_layer_action=str(
                    self._profile.get("final_layer_action", "repeat_patrol")
                ),
                patrol_start_layer=self.patrol_range_locked()[0],
                patrol_end_layer=self.patrol_range_locked()[1],
                patrol_range_set=bool(
                    self._profile.get("patrol_start_layer")
                    and self._profile.get("patrol_end_layer")
                ),
            )

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        # Runtime-only: every launch uses the profile's safe startup default.
        with self._lock:
            self._enabled = bool(enabled)

    def can_start(self) -> bool:
        with self._lock:
            route = self._profile.get("route_order", [])
            if not route:
                # Nothing recorded is still startable: the worker stands still
                # and only attacks (e.g. Fixed Attack / YOLO farming).
                return True
            # Start as soon as every routed layer has at least one recorded
            # action point.  Adaptive coordinate_v2 is NOT required to start -
            # the UI labels a layer "legacy layout" when it should be
            # re-recorded, but patrol is enabled regardless.
            return all(self.layer_is_complete(name) for name in route)

    def selected_layer(self) -> str:
        with self._lock:
            return self._selected_layer

    def select_layer(self, layer_name: str) -> None:
        """Select an existing calibration row without changing patrol data."""

        with self._lock:
            if layer_name not in self._profile.get("layers", {}):
                raise ValueError(f"unknown layer: {layer_name}")
            self._selected_layer = layer_name

    def reset_recording(self) -> None:
        """Clear all recorded route points and return to one empty layer."""

        with self._lock:
            # The map name is a user-assigned label for the current map and
            # may have been edited on disk while the app was running.  Reset
            # only the route/layers, never clobber the map identity label.
            try:
                on_disk = json.loads(
                    self.profile_path.read_text(encoding="utf-8")
                )
                map_name = str(on_disk.get("map_name", "")).strip()
            except (OSError, ValueError, TypeError):
                map_name = str(self._profile.get("map_name", "")).strip()
            self._enabled = False
            self._selected_layer = "layer1"
            self._profile["map_name"] = map_name
            self._profile["route_order"] = []
            self._profile["first_layer"] = "layer1"
            # A reset wipes the patrol range too - the fresh recording starts
            # over without a stale start/end selection.
            self._profile["patrol_start_layer"] = ""
            self._profile["patrol_end_layer"] = ""
            self._profile["layers"] = {
                "layer1": {
                    "y_tolerance": 0.020000,
                    "calibration_status": "awaiting_left_rope_right",
                }
            }
            rope = self._profile.setdefault("rope", {})
            near_range = float(rope.get("near_range", 0.022500))
            inner_range = float(rope.get("inner_range", near_range))
            outer_range = float(rope.get("outer_range", near_range))
            self._profile["rope"] = {
                "x": 0.500000,
                "near_range": near_range,
                "inner_range": inner_range,
                "outer_range": outer_range,
            }
            self._persist_locked()

    def map_name(self) -> str:
        with self._lock:
            return str(self._profile.get("map_name", "")).strip()

    def rope_zone(self) -> tuple[Optional[float], float]:
        """Return the configured (rope X, inner climb gap) in analysis units."""

        with self._lock:
            rope = self._profile.get("rope", {})
            raw_x = rope.get("x")
            rope_x = float(raw_x) if raw_x is not None else None
            near = float(rope.get("near_range", 0.022500))
            inner = float(rope.get("inner_range", near))
            return rope_x, min(near, max(0.0, inner))

    def first_layer(self) -> str:
        with self._lock:
            return str(self._profile.get("first_layer", "")).strip()

    def snapshot_layers(self) -> dict[str, Any]:
        return self.snapshot().layers

    def _adaptive_ready_locked(self, name: str) -> bool:
        """True when every recorded action point on *name* is adaptive.

        A layer is usable with any subset of Left/Rope/Right; legacy ratio-only
        points (no coordinate_v2) are not adaptive and must be re-recorded.
        """

        layer = self._profile.get("layers", {}).get(name, {})
        present = _layer_present_actions(layer)
        if not present:
            return False
        return all(
            isinstance(layer[point].get("coordinate_v2"), dict)
            for point in present
        )

    def layer_is_adaptive(self, layer_name: Optional[str] = None) -> bool:
        with self._lock:
            return self._adaptive_ready_locked(layer_name or self._selected_layer)

    def patrol_range_locked(self) -> tuple[str, str]:
        """Effective contiguous patrol floor range (start, end).

        Defaults to the recorded bottom/top floors when the range was never
        set; ``patrol_start_layer``/``patrol_end_layer`` keys in the profile
        override it.  A single floor is allowed (start == end).
        """

        route = self._sorted_layer_names_locked()
        if not route:
            return "", ""
        start = str(self._profile.get("patrol_start_layer", "")).strip()
        end = str(self._profile.get("patrol_end_layer", "")).strip()
        if not start or start not in route:
            start = route[0]
        if not end or end not in route:
            end = route[-1]
        return start, end

    def patrol_range(self) -> tuple[str, str]:
        with self._lock:
            return self.patrol_range_locked()

    def set_patrol_range(self, start_layer: str, end_layer: str) -> None:
        """Select the contiguous patrol floor range (start .. end).

        Both floors must be recorded layers and ``start`` must not be above
        ``end`` by floor number (a single floor is allowed).  The selection
        persists; the movement worker patrols only this contiguous range and
        returns to it when the character falls outside it.
        """

        with self._lock:
            layers = self._profile.get("layers", {})
            for name in (start_layer, end_layer):
                if name and name not in layers:
                    raise ValueError(f"layer not recorded: {name}")
            if start_layer and end_layer:
                if _layer_number(start_layer) > _layer_number(end_layer):
                    raise ValueError(
                        f"patrol start {start_layer} must not be above end {end_layer}"
                    )
            self._profile["patrol_start_layer"] = start_layer
            self._profile["patrol_end_layer"] = end_layer
            self._persist_locked()

    def layer_is_patrol_ready(self, layer_name: Optional[str] = None) -> bool:
        """Return whether the layer's recorded action points are all adaptive."""

        with self._lock:
            return self._adaptive_ready_locked(layer_name or self._selected_layer)

    def endpoint(self, layer: str, boundary: PointKind) -> Optional[RecordedEndpoint]:
        with self._lock:
            if boundary == "rope_pos" and layer == self._final_layer_name_locked():
                return None
            value = self._profile.get("layers", {}).get(layer, {}).get(boundary)
            if not isinstance(value, dict) or "x" not in value or "y" not in value:
                return None
            return RecordedEndpoint(layer, boundary, float(value["x"]), float(value["y"]))

    def clear_endpoint(self, layer: str, boundary: PointKind) -> bool:
        """Remove a recorded point (the UI long-press unlock clears it).

        Recomputes the layer's calibration status and route membership, then
        persists - mirroring ``record_endpoint``'s bookkeeping.  Returns True
        when a point was actually removed.
        """

        if boundary not in REQUIRED_LAYER_POINTS:
            raise ValueError(f"unsupported patrol point: {boundary}")
        with self._lock:
            layers = self._profile.get("layers", {})
            layer_data = layers.get(layer)
            if not isinstance(layer_data, dict):
                return False
            removed = layer_data.pop(boundary, None) is not None
            if removed:
                any_action = bool(_layer_present_actions(layer_data))
                has_edges = self._layer_has_points_locked(
                    layer, PATROL_EDGE_POINTS
                )
                if layer == self._final_layer_name_locked() and has_edges:
                    layer_data["calibration_status"] = "final_layer_ready"
                elif self._layer_has_points_locked(layer, ("rope_pos",)):
                    layer_data["calibration_status"] = "complete"
                elif any_action:
                    layer_data["calibration_status"] = "ready"
                else:
                    layer_data["calibration_status"] = "awaiting_left_rope_right"
                route = self._profile.get("route_order", [])
                if not any_action and layer in route:
                    route.remove(layer)
                self._persist_locked()
            return removed

    def layer_for_y(self, player_y: float) -> Optional[str]:
        """Resolve an active route layer solely from calibrated Y."""

        with self._lock:
            layers = self._profile.get("layers", {})
            candidates: list[tuple[float, str]] = []
            for name in self._profile.get("route_order", list(layers)):
                layer = layers.get(name)
                if not isinstance(layer, dict) or "layer_y" not in layer:
                    continue
                tolerance = float(layer.get("y_tolerance", 0.020000))
                band = _layer_y_band(layer, tolerance)
                if band is None:
                    continue
                if band[0] - 1e-9 <= player_y <= band[1] + 1e-9:
                    candidates.append((0.0, name))
            return min(candidates)[1] if candidates else None

    def layer_for_world_y(self, world_y: float) -> Optional[str]:
        """Resolve a layer from scroll-compensated minimap structure Y."""

        with self._lock:
            layers = self._profile.get("layers", {})
            candidates: list[tuple[float, str]] = []
            for name in self._profile.get("route_order", list(layers)):
                layer = layers.get(name)
                if not isinstance(layer, dict) or "layer_world_y" not in layer:
                    continue
                tolerance = float(layer.get("world_y_tolerance", 0.75))
                band = _layer_world_y_band(layer, tolerance)
                if band is None:
                    continue
                if band[0] - 1e-9 <= world_y <= band[1] + 1e-9:
                    candidates.append((0.0, name))
            return min(candidates)[1] if candidates else None

    def layer_is_complete(self, layer_name: Optional[str] = None) -> bool:
        """True when the layer has at least one recorded action point."""

        with self._lock:
            name = layer_name or self._selected_layer
            layer = self._profile.get("layers", {}).get(name, {})
            return bool(_layer_present_actions(layer))

    def final_layer_name(self) -> Optional[str]:
        with self._lock:
            return self._final_layer_name_locked()

    def _final_layer_name_locked(self) -> Optional[str]:
        layers = self._profile.get("layers", {})
        return next(reversed(layers), None) if layers else None

    def _layer_has_points_locked(
        self,
        layer_name: str,
        points: tuple[PointKind, ...],
        *,
        adaptive: bool = False,
    ) -> bool:
        layer = self._profile.get("layers", {}).get(layer_name, {})
        for point_name in points:
            point = layer.get(point_name)
            if not isinstance(point, dict) or "x" not in point or "y" not in point:
                return False
            if adaptive and not isinstance(point.get("coordinate_v2"), dict):
                return False
        return True

    def record_endpoint(
        self,
        boundary: PointKind,
        player_x: float,
        player_y: float,
        layout: Optional[CoordinateLayout] = None,
        world_y: Optional[float] = None,
        tracking_confidence: Optional[float] = None,
    ) -> RecordedEndpoint:
        """Record Left/Rope/Right for the selected calibration layer."""

        if boundary not in REQUIRED_LAYER_POINTS:
            raise ValueError(f"unsupported patrol point: {boundary}")
        with self._lock:
            layer_name = self._selected_layer
            layers = self._profile.setdefault("layers", {})
            layer = layers.setdefault(layer_name, {"y_tolerance": 0.020000})
            if boundary == "rope_pos" and layer_name == self._final_layer_name_locked():
                raise ValueError("Rope cannot be recorded on the final layer")
            match = re.search(r"(\d+)$", layer_name)
            lower_name = f"layer{int(match.group(1)) - 1}" if match and int(
                match.group(1)
            ) > 1 else None
            lower_layer = layers.get(lower_name, {}) if lower_name else {}
            # The first recorded point establishes a layer-level world Y.
            # Horizontal movement across a repeating minimap can make phase
            # correlation briefly lock onto another identical platform. Once
            # established, explicit recordings on this selected layer inherit
            # its canonical Y instead of treating that visual alias as a new
            # layer. Keep the measured value separately for diagnostics.
            observed_world_y = float(world_y) if world_y is not None else None
            canonical_world_y = (
                float(layer["layer_world_y"])
                if "layer_world_y" in layer else observed_world_y
            )
            if isinstance(lower_layer, dict):
                # 层序检查仅作警告，不再拒绝录制：某些地图/分辨率下世界 Y
                # 排序与预期不符，拒绝会让用户无法录制（如 layer5 的世界 Y
                # 不在 layer4 之下）。录制由用户负责，这里只提醒。
                if canonical_world_y is not None and "layer_world_y" in lower_layer:
                    lower_world_y = float(lower_layer["layer_world_y"])
                    separation = max(
                        float(lower_layer.get("world_y_tolerance", 0.75)),
                        float(layer.get("world_y_tolerance", 0.75)),
                    )
                    if canonical_world_y >= lower_world_y - separation:
                        LOG.warning(
                            "%s recorded with world Y %.6f, not below %s "
                            "(%.6f +- %.3f); layer order may be wrong",
                            layer_name, canonical_world_y, lower_name,
                            lower_world_y, separation,
                        )
                elif world_y is None and "layer_y" in lower_layer:
                    lower_y = float(lower_layer["layer_y"])
                    separation = max(
                        float(lower_layer.get("y_tolerance", 0.020000)),
                        float(layer.get("y_tolerance", 0.020000)),
                    )
                    if float(player_y) >= lower_y - separation:
                        LOG.warning(
                            "%s recorded with Y %.6f, not below %s "
                            "(Y=%.6f +- %.3f); layer order may be wrong",
                            layer_name, float(player_y), lower_name,
                            lower_y, separation,
                        )
            if "layer_y" in layer and layout is None and world_y is None:
                gap = abs(float(layer["layer_y"]) - float(player_y))
                tolerance = float(layer.get("y_tolerance", 0.020000))
                if gap > tolerance:
                    raise ValueError(
                        f"player Y {player_y:.6f} is not on selected {layer_name} "
                        f"(Y={float(layer['layer_y']):.6f} ±{tolerance:.6f})"
                    )
            point = layer.setdefault(boundary, {})
            # 录制坐标钳制到小地图有效范围 [0.02, 0.98]：边缘附近的标记可能
            # 换算出越界值，越界目标会让巡逻永远追着地图外走。
            point["x"] = round(max(0.02, min(0.98, float(player_x))), 6)
            point["y"] = round(max(0.02, min(0.98, float(player_y))), 6)
            point["source"] = "manual-ui"
            if canonical_world_y is not None:
                point["world_y"] = round(canonical_world_y, 6)
                if observed_world_y is not None:
                    point["observed_world_y"] = round(observed_world_y, 6)
                point["tracking_confidence"] = round(
                    float(tracking_confidence or 0.0), 6
                )
            if layout is not None:
                stable_x, stable_y = layout.stable_point(player_x, player_y)
                point["coordinate_v2"] = {
                    "x_diamond": round(stable_x, 6),
                    "y_diamond": round(stable_y, 6),
                    "recorded_layout": layout.as_dict(),
                }
                layer["y_tolerance_diamonds"] = round(
                    float(layer.get("y_tolerance", 0.020000))
                    * layout.analysis_height / max(1.0, layout.diamond_height),
                    6,
                )
            manual_y_values = [
                float(layer[name]["y"])
                for name in REQUIRED_LAYER_POINTS
                if isinstance(layer.get(name), dict)
                and layer[name].get("source") == "manual-ui"
                and "y" in layer[name]
            ]
            if manual_y_values:
                # Layer Y is the AVERAGE of the recorded points (Left/Rope/
                # Right that exist).  A median of two points degenerates to
                # the larger one, biasing the layer band toward one edge of
                # the platform and making climb arrival detection miss.
                layer["layer_y"] = round(
                    float(sum(manual_y_values)) / len(manual_y_values), 6
                )
                layer["layer_y_source"] = "manual-ui"
            manual_world_values = [
                float(layer[name]["world_y"])
                for name in REQUIRED_LAYER_POINTS
                if isinstance(layer.get(name), dict)
                and layer[name].get("source") == "manual-ui"
                and "world_y" in layer[name]
            ]
            if manual_world_values:
                layer["layer_world_y"] = round(
                    float(sum(manual_world_values)) / len(manual_world_values), 6
                )
                layer["world_y_tolerance"] = round(float(
                    layer.get("world_y_tolerance", 0.75)
                ), 6)
            # The route includes a layer as soon as it has any action point -
            # record only the points you want patrolled (Left and/or Right
            # and/or Rope).
            any_action = bool(_layer_present_actions(layer))
            has_edges = self._layer_has_points_locked(
                layer_name, PATROL_EDGE_POINTS
            )
            if layer_name == self._final_layer_name_locked() and has_edges:
                layer["calibration_status"] = "final_layer_ready"
            elif self._layer_has_points_locked(layer_name, ("rope_pos",)):
                layer["calibration_status"] = "complete"
            elif any_action:
                layer["calibration_status"] = "ready"
            else:
                layer["calibration_status"] = "awaiting_left_rope_right"
            if any_action:
                route = self._profile.setdefault("route_order", [])
                if layer_name not in route:
                    route.append(layer_name)
            self._persist_locked()
            return RecordedEndpoint(
                layer_name, boundary, float(point["x"]), float(point["y"])
            )

    def _project_layers_locked(
        self, layers: dict[str, Any], layout: CoordinateLayout
    ) -> None:
        for layer in layers.values():
            if not isinstance(layer, dict):
                continue
            projected_y: list[float] = []
            for point_name in REQUIRED_LAYER_POINTS:
                point = layer.get(point_name)
                coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
                if not isinstance(coordinate, dict):
                    continue
                try:
                    x, y = layout.project(
                        float(coordinate["x_diamond"]),
                        float(coordinate["y_diamond"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                # 钳制到小地图有效范围 [0.02, 0.98]：菱形尺寸随分辨率变化时
                # 投影可能越界（如 -0.134），不可达目标会让角色永远追着地图
                # 边缘外走。钳制后目标始终可达，相位能正常完成。
                point["x"] = round(max(0.02, min(0.98, x)), 6)
                point["y"] = round(max(0.02, min(0.98, y)), 6)
                projected_y.append(y)
            if projected_y:
                # Same average rule as the recorded layer_y: the projected
                # layer level is the mean of the projected points so the
                # arrival band centers on the platform, not one edge.
                layer["layer_y"] = round(
                    float(sum(projected_y)) / len(projected_y), 6
                )
            if "y_tolerance_diamonds" in layer:
                layer["y_tolerance"] = round(
                    float(layer["y_tolerance_diamonds"])
                    * layout.diamond_height / layout.analysis_height,
                    6,
                )

    def add_layer_above(self) -> str:
        """Always create and select a new highest numeric layer."""

        with self._lock:
            layers = self._profile.setdefault("layers", {})
            numeric_layers = [
                int(match.group(1))
                for name in layers
                if (match := re.search(r"(\d+)$", name)) is not None
            ]
            next_number = max(numeric_layers, default=0) + 1
            next_name = f"layer{next_number}"
            # Assignment is intentionally unconditional because next_number is
            # above every existing numeric layer and therefore cannot replace
            # a calibrated row.
            layers[next_name] = {
                "y_tolerance": 0.020000,
                "calibration_status": "awaiting_left_rope_right",
            }
            self._selected_layer = next_name
            self._enabled = False
            self._persist_locked()
            return next_name

    def _persist_locked(self) -> None:
        temporary_path = self.profile_path.with_suffix(self.profile_path.suffix + ".tmp")
        text = json.dumps(self._profile, ensure_ascii=False, indent=2) + "\n"
        temporary_path.write_text(text, encoding="utf-8")
        temporary_path.replace(self.profile_path)


__all__ = [
    "ACTION_POINTS",
    "Boundary",
    "CoordinateLayout",
    "PatrolController",
    "PatrolSnapshot",
    "PointKind",
    "PATROL_EDGE_POINTS",
    "REQUIRED_LAYER_POINTS",
    "RecordedEndpoint",
    "_layer_present_actions",
]
