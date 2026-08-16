"""Thread-safe patrol controls and persistent multi-layer calibration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
from typing import Any, Literal, Optional


PointKind = Literal["left_most_pos", "rope_pos", "right_most_pos"]
Boundary = Literal["left_most_pos", "right_most_pos"]
REQUIRED_LAYER_POINTS: tuple[PointKind, ...] = (
    "left_most_pos", "rope_pos", "right_most_pos"
)
PATROL_EDGE_POINTS: tuple[PointKind, ...] = (
    "left_most_pos", "right_most_pos"
)


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
        self._lock = threading.RLock()

    def snapshot(self, layout: Optional[CoordinateLayout] = None) -> PatrolSnapshot:
        with self._lock:
            layers = deepcopy(self._profile.get("layers", {}))
            final_layer = self._final_layer_name_locked()
            # A saved rope on the highest layer is retained on disk so it can
            # become useful if another layer is added, but it is invisible to
            # current UI and movement logic while that layer remains final.
            if final_layer is not None and isinstance(layers.get(final_layer), dict):
                layers[final_layer].pop("rope_pos", None)
            if layout is not None:
                self._project_layers_locked(layers, layout)
            return PatrolSnapshot(
                enabled=self._enabled,
                selected_layer=self._selected_layer,
                route_order=tuple(self._profile.get("route_order", [])),
                layers=layers,
                climbing_enabled=bool(self._profile.get("climbing_enabled", True)),
                final_layer_action=str(
                    self._profile.get("final_layer_action", "repeat_patrol")
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
            layers = self._profile.get("layers", {})
            layer_names = list(layers)
            final_layer = self._final_layer_name_locked()
            if not route or not layer_names or final_layer is None:
                return False
            if any(name not in route for name in layer_names):
                return False
            return all(
                self._layer_has_points_locked(
                    name,
                    PATROL_EDGE_POINTS if name == final_layer else REQUIRED_LAYER_POINTS,
                    adaptive=True,
                )
                for name in route
            )

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

    def layer_is_adaptive(self, layer_name: Optional[str] = None) -> bool:
        with self._lock:
            name = layer_name or self._selected_layer
            points = (
                PATROL_EDGE_POINTS
                if name == self._final_layer_name_locked()
                else REQUIRED_LAYER_POINTS
            )
            return self._layer_has_points_locked(
                name, points, adaptive=True
            )

    def layer_is_patrol_ready(self, layer_name: Optional[str] = None) -> bool:
        """Return whether a layer has adaptive points required for its route role."""

        with self._lock:
            name = layer_name or self._selected_layer
            points = (
                PATROL_EDGE_POINTS
                if name == self._final_layer_name_locked()
                else REQUIRED_LAYER_POINTS
            )
            return self._layer_has_points_locked(name, points, adaptive=True)

    def endpoint(self, layer: str, boundary: PointKind) -> Optional[RecordedEndpoint]:
        with self._lock:
            if boundary == "rope_pos" and layer == self._final_layer_name_locked():
                return None
            value = self._profile.get("layers", {}).get(layer, {}).get(boundary)
            if not isinstance(value, dict) or "x" not in value or "y" not in value:
                return None
            return RecordedEndpoint(layer, boundary, float(value["x"]), float(value["y"]))

    def layer_for_y(self, player_y: float) -> Optional[str]:
        """Resolve an active route layer solely from calibrated Y."""

        with self._lock:
            layers = self._profile.get("layers", {})
            candidates: list[tuple[float, str]] = []
            for name in self._profile.get("route_order", list(layers)):
                layer = layers.get(name)
                if not isinstance(layer, dict) or "layer_y" not in layer:
                    continue
                gap = abs(float(layer["layer_y"]) - float(player_y))
                if gap <= float(layer.get("y_tolerance", 0.020000)):
                    candidates.append((gap, name))
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
                gap = abs(float(layer["layer_world_y"]) - float(world_y))
                tolerance = float(layer.get("world_y_tolerance", 0.75))
                if gap <= tolerance:
                    candidates.append((gap, name))
            return min(candidates)[1] if candidates else None

    def layer_is_complete(self, layer_name: Optional[str] = None) -> bool:
        with self._lock:
            name = layer_name or self._selected_layer
            points = (
                PATROL_EDGE_POINTS
                if name == self._final_layer_name_locked()
                else REQUIRED_LAYER_POINTS
            )
            return self._layer_has_points_locked(name, points)

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
                if canonical_world_y is not None and "layer_world_y" in lower_layer:
                    lower_world_y = float(lower_layer["layer_world_y"])
                    separation = max(
                        float(lower_layer.get("world_y_tolerance", 0.75)),
                        float(layer.get("world_y_tolerance", 0.75)),
                    )
                    if canonical_world_y >= lower_world_y - separation:
                        raise ValueError(
                            f"{layer_name} must be above {lower_name}: "
                            f"world Y must be below {lower_world_y - separation:.6f}"
                        )
                elif world_y is None and "layer_y" in lower_layer:
                    lower_y = float(lower_layer["layer_y"])
                    separation = max(
                        float(lower_layer.get("y_tolerance", 0.020000)),
                        float(layer.get("y_tolerance", 0.020000)),
                    )
                    if float(player_y) >= lower_y - separation:
                        raise ValueError(
                            f"{layer_name} must be above {lower_name}: "
                            f"Y must be below {lower_y - separation:.6f}"
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
            point["x"] = round(float(player_x), 6)
            point["y"] = round(float(player_y), 6)
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
                layer["layer_y"] = round(float(sorted(manual_y_values)[
                    len(manual_y_values) // 2
                ]), 6)
                layer["layer_y_source"] = "manual-ui"
            manual_world_values = [
                float(layer[name]["world_y"])
                for name in REQUIRED_LAYER_POINTS
                if isinstance(layer.get(name), dict)
                and layer[name].get("source") == "manual-ui"
                and "world_y" in layer[name]
            ]
            if manual_world_values:
                layer["layer_world_y"] = round(float(sorted(manual_world_values)[
                    len(manual_world_values) // 2
                ]), 6)
                layer["world_y_tolerance"] = round(float(
                    layer.get("world_y_tolerance", 0.75)
                ), 6)
            patrol_edges_ready = self._layer_has_points_locked(
                layer_name, PATROL_EDGE_POINTS
            )
            if layer_name == self._final_layer_name_locked() and patrol_edges_ready:
                layer["calibration_status"] = "final_layer_ready"
            elif self.layer_is_complete(layer_name):
                layer["calibration_status"] = "complete"
            elif patrol_edges_ready:
                layer["calibration_status"] = "final_layer_ready"
            else:
                layer["calibration_status"] = "awaiting_left_rope_right"
            if patrol_edges_ready:
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
                point["x"] = round(x, 6)
                point["y"] = round(y, 6)
                projected_y.append(y)
            if projected_y:
                layer["layer_y"] = round(
                    sorted(projected_y)[len(projected_y) // 2], 6
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
    "Boundary",
    "CoordinateLayout",
    "PatrolController",
    "PatrolSnapshot",
    "PointKind",
    "PATROL_EDGE_POINTS",
    "REQUIRED_LAYER_POINTS",
    "RecordedEndpoint",
]
