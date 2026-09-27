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


# The same upper-band factor as ``movement_worker.LAYER_UP_REACH_FACTOR`` (the operator, 2026-09-18:
# "narrow down layer upper band a little bit, make it 0.7").  The bands must mean the same thing in both
# modules: this one answers the panel's "is the marker on a recorded layer" question.
LAYER_UP_REACH_FACTOR = 0.7


def _layer_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    values = _layer_point_ys(layer)
    if not values and isinstance(layer, dict) and "layer_y" in layer:
        values = [float(layer["layer_y"])]
    if not values:
        return None
    if len(values) == 1:
        # Keep the UI/controller interpretation exactly aligned with the
        # movement worker: a rope-only layer is only [y, y + 0.002].  Jump
        # points never enter _layer_point_ys, so their vertical coordinate
        # cannot widen or shift a layer band.
        only_y = values[0]
        return only_y, only_y + 0.002
    # The margin above covers climb/drop arrival movement, scaled by the upper-band factor. A smaller
    # one-third margin below the confirmed layer base absorbs OpenCV marker precision noise without
    # excessive overlap with the layer below.
    effective_tolerance = max(0.0, float(tolerance))
    return (
        min(values) - effective_tolerance * LAYER_UP_REACH_FACTOR,
        max(values) + effective_tolerance / 3.0,
    )


def _coherent_observed_world_values(layer: Any) -> list[float]:
    """Use raw per-point world Y only when it agrees with diamond-space Y."""

    readings: list[tuple[float, float]] = []
    for point_name in ("left_most_pos", "rope_pos", "right_most_pos"):
        point = layer.get(point_name) if isinstance(layer, dict) else None
        coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
        if (not isinstance(point, dict)
                or not isinstance(coordinate, dict)
                or "observed_world_y" not in point
                or "y_diamond" not in coordinate
                or float(point.get("tracking_confidence", 0.0)) < 0.12):
            continue
        readings.append((
            float(point["observed_world_y"]),
            float(coordinate["y_diamond"]),
        ))
    if len(readings) < 2:
        return []
    offsets = [world_y - diamond_y for world_y, diamond_y in readings]
    if max(offsets) - min(offsets) > 0.35:
        return []
    return [world_y for world_y, _ in readings]


def _layer_world_y_band(layer: Any, tolerance: float) -> Optional[tuple[float, float]]:
    values = _coherent_observed_world_values(layer)
    if not values:
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


def _diamond_geometry_matches(recorded_layout: Any, layout: Any) -> bool:
    """True when the recorded yellow-diamond size is the live one.

    The pixel size of the yellow player diamond can fluctuate by about one
    pixel between OpenCV frames, so a small difference stays "same geometry"
    (re-projecting through that noise previously shifted a saved rope).  But
    a recorded diamond far from the live one - for example the 15x7 blob a
    merged map-art run produced while the real diamond is 6x6 - is a
    genuinely different geometry: the stored normalized x/y was computed with
    the wrong divisor, so the point must be re-projected through its stable
    diamond coordinate instead of being trusted.
    """

    try:
        recorded = float(recorded_layout["diamond_width"])
        live = max(1.0, float(layout.diamond_width))
    except (KeyError, TypeError, ValueError, AttributeError):
        return True
    return abs(recorded - live) <= max(1.5, live * 0.35)


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

    def __init__(
        self, profile_path: Path, profile: dict[str, Any], *,
        config_store: Any = None,
    ) -> None:
        self.profile_path = Path(profile_path)
        self.config_store = config_store
        self._profile = deepcopy(profile)
        self._enabled = bool(profile.get("patrol_enabled", False))
        layers = profile.get("layers", {})
        # The calibration row starts on the physical final (top) layer, not
        # the last JSON/recording-order entry.  Recording order is arbitrary:
        # a lower rope can be saved after a final-layer endpoint, which used
        # to make the assistant open on the wrong row.
        self._selected_layer = self._final_layer_name_locked()
        # Contiguous patrol floor range lives in the profile
        # (``patrol_start_layer`` / ``patrol_end_layer``) so recordings, the
        # UI and the movement worker all see the same persisted selection.
        self._lock = threading.RLock()
        # The last inverted range this profile reported, so the warning below is said once per
        # distinct pair instead of on every frame.
        self._inverted_range_reported: tuple[str, str] = ("", "")

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
        """Whether the selected patrol route has both horizontal endpoints."""

        return not self.validate_patrol_route()

    def _patrol_route_layers_locked(self) -> list[str]:
        """Physical layers inside the selected contiguous patrol range."""

        start, end = self.patrol_range_locked()
        if not start or not end:
            return []
        start_number, end_number = _layer_number(start), _layer_number(end)
        return [
            name for name in sorted(self._profile.get("layers", {}), key=_layer_number)
            if start_number <= _layer_number(name) <= end_number
        ]

    def validate_patrol_route(self) -> tuple[str, ...]:
        """Return human-readable errors for the selected patrol route.

        Recording stays deliberately loose: a layer outside the route may have
        only a rope, only one endpoint, or no point at all.  The stricter
        left/right requirement is applied only immediately before a route is
        started, after the operator has chosen its start/end layers.
        """

        with self._lock:
            route = self._patrol_route_layers_locked()
            if not route:
                return ("未选择有效的巡逻楼层。",)
            errors: list[str] = []
            layers = self._profile.get("layers", {})
            for name in route:
                layer = layers.get(name)
                missing = [
                    label for point, label in (
                        ("left_most_pos", "最左"),
                        ("right_most_pos", "最右"),
                    )
                    if not (isinstance(layer, dict)
                            and isinstance(layer.get(point), dict)
                            and "x" in layer[point] and "y" in layer[point])
                ]
                if missing:
                    errors.append(f"{name} 缺少{'、'.join(missing)}点")
            return tuple(errors)

    def selected_layer(self) -> str:
        with self._lock:
            return self._selected_layer

    def select_layer(self, layer_name: str) -> None:
        """Select an existing calibration row without changing patrol data."""

        with self._lock:
            if layer_name not in self._profile.get("layers", {}):
                raise ValueError(f"未知楼层：{layer_name}")
            self._selected_layer = layer_name

    def reset_recording(self) -> None:
        """Clear all recorded route points and return to one empty layer."""

        with self._lock:
            # The map name is a user-assigned label for the current map and
            # may have been edited on disk while the app was running.  Reset
            # only the route/layers, never clobber the map identity label.
            try:
                on_disk = (
                    self.config_store.read_section("recording")
                    if self.config_store is not None
                    else json.loads(self.profile_path.read_text(encoding="utf-8"))
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

        The range is selected from the physical layer list, not
        ``route_order``.  ``route_order`` deliberately contains only layers
        with at least one recorded action, whereas the UI must immediately
        show and persist a newly added (not-yet-recorded) top layer.  Movement
        still filters to action-bearing layers before it moves, so this does
        not make an empty layer patrolable.

        Defaults to the lowest/highest existing floors when the range was
        never set; ``patrol_start_layer``/``patrol_end_layer`` keys in the
        profile override it.  A single floor is allowed (start == end).
        """

        layers = sorted(
            self._profile.get("layers", {}), key=_layer_number
        )
        if not layers:
            return "", ""
        start = str(self._profile.get("patrol_start_layer", "")).strip()
        end = str(self._profile.get("patrol_end_layer", "")).strip()
        if not start or start not in layers:
            start = layers[0]
        if not end or end not in layers:
            end = layers[-1]
        # The range is stored lowest floor -> highest floor.  A profile written while the layers
        # were numbered differently can hold the pair the other way round (observed: "layer2 ->
        # layer1" together with a completely silent stand-still patrol).  Both names describe the
        # SAME set of floors, so use the canonical order and say so once, loudly.
        if _layer_number(start) > _layer_number(end):
            if self._inverted_range_reported != (start, end):
                self._inverted_range_reported = (start, end)
                LOG.warning(
                    "巡逻范围 %s -> %s 方向相反（起点在终点上方，切片结果为空，"
                    "巡逻会原地站立）；已按 %s -> %s 处理，请在界面上重新确认范围",
                    start, end, end, start,
                )
            start, end = end, start
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
                    raise ValueError(f"该楼层尚未录制：{name}")
            if start_layer and end_layer:
                if _layer_number(start_layer) > _layer_number(end_layer):
                    raise ValueError(
                        f"巡逻起始楼层 {start_layer} 不能高于结束楼层 {end_layer}"
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
            raise ValueError(f"不支持的点类型：{boundary}")
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
        return max(layers, key=_layer_number) if layers else None

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
            raise ValueError(f"不支持的点类型：{boundary}")
        with self._lock:
            layer_name = self._selected_layer
            if layer_name not in self._profile.get("layers", {}):
                raise ValueError(
                    "没有可录制的楼层；请先点击「添加楼层」"
                    if not layer_name else f"所选楼层 {layer_name} 不存在"
                )
            layers = self._profile["layers"]
            layer = layers[layer_name]
            if boundary == "rope_pos" and layer_name == self._final_layer_name_locked():
                raise ValueError(
                    "最上层无法录制绳索点：绳索用于爬到上一层，"
                    "当前楼层已是最上面的巡逻楼层。"
                )
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
                        f"角色当前不在所选 {layer_name} 上"
                        f"（角色 Y={player_y:.6f}，该层 Y={float(layer['layer_y']):.6f} "
                        f"±{tolerance:.6f}）；请先走到该楼层再录制。"
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
            # Recording is intentionally permissive.  A partial point may be
            # useful later, but only the selected patrol range is checked for
            # its required Left/Right endpoints at Start.
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

    def record_jump_point(
        self, player_x: float, player_y: float, *, direction: str,
        layout: Optional[CoordinateLayout] = None,
    ) -> RecordedEndpoint:
        """Add one immutable directional jump trigger.

        Storage preserves recording order.  The layer axis is responsible for
        presenting points in X order, so persisted JSON order is never a
        validity requirement for an otherwise empty or partially recorded
        layer.
        """

        direction = str(direction).casefold()
        if direction not in ("left", "right"):
            raise ValueError("跳点方向必须是 left 或 right")

        with self._lock:
            layer_name = self._selected_layer
            layer = self._profile.get("layers", {}).get(layer_name)
            if not isinstance(layer, dict):
                raise ValueError("没有可录制的楼层；请先点击「添加楼层」")
            point: dict[str, Any] = {
                "x": round(max(0.02, min(0.98, float(player_x))), 6),
                "y": round(max(0.02, min(0.98, float(player_y))), 6),
                "direction": direction,
                "source": "manual-ui",
            }
            if layout is not None:
                stable_x, stable_y = layout.stable_point(player_x, player_y)
                point["coordinate_v2"] = {
                    "x_diamond": round(stable_x, 6),
                    "y_diamond": round(stable_y, 6),
                    "recorded_layout": layout.as_dict(),
                }
            points = layer.setdefault("jump_points", [])
            if not isinstance(points, list):
                points = layer["jump_points"] = []
            points.append(point)
            self._persist_locked()
            return RecordedEndpoint(layer_name, "jump_point", point["x"], point["y"])

    def delete_jump_point(self, layer_name: str, index: int) -> bool:
        """Delete one locked jump point by its stored list index.

        Jump points are immutable once recorded: editing one would silently
        make its Y trigger disagree with the actual platform.  The UI can
        therefore only remove a point and let the operator record a new one.
        """

        with self._lock:
            layer = self._profile.get("layers", {}).get(layer_name)
            if not isinstance(layer, dict):
                return False
            points = layer.get("jump_points")
            if not isinstance(points, list) or not (0 <= int(index) < len(points)):
                return False
            points.pop(int(index))
            if not points:
                layer.pop("jump_points", None)
            self._persist_locked()
            return True

    def _project_layers_locked(
        self, layers: dict[str, Any], layout: CoordinateLayout
    ) -> None:
        for layer in layers.values():
            if not isinstance(layer, dict):
                continue
            projected_y: list[float] = []
            adaptive_points = 0
            incompatible_layout = False
            for point_name in REQUIRED_LAYER_POINTS:
                point = layer.get(point_name)
                coordinate = point.get("coordinate_v2") if isinstance(point, dict) else None
                if not isinstance(coordinate, dict):
                    continue
                adaptive_points += 1
                recorded_layout = coordinate.get("recorded_layout")
                same_analysis_frame = False
                if isinstance(recorded_layout, dict):
                    try:
                        width_ratio = (
                            float(recorded_layout["analysis_width"])
                            / max(1.0, layout.analysis_width)
                        )
                        height_ratio = (
                            float(recorded_layout["analysis_height"])
                            / max(1.0, layout.analysis_height)
                        )
                    except (KeyError, TypeError, ValueError):
                        width_ratio = height_ratio = 1.0
                    try:
                        recorded_width = float(recorded_layout["analysis_width"])
                        recorded_height = float(recorded_layout["analysis_height"])
                        fills_analysis = (
                            abs(float(recorded_layout["canvas_left"])) <= 1.0
                            and abs(float(recorded_layout["canvas_top"])) <= 1.0
                            and float(recorded_layout["canvas_width"])
                            >= recorded_width * 0.98
                            and float(recorded_layout["canvas_height"])
                            >= recorded_height * 0.98
                        )
                    except (KeyError, TypeError, ValueError):
                        fills_analysis = False
                    if fills_analysis and (
                            not 0.65 <= width_ratio <= 1.55
                            or not 0.65 <= height_ratio <= 1.55
                    ):
                        # Older recordings could accidentally store the
                        # top-left SEARCH REGION as recorded_layout.  It is
                        # not the minimap border, so projecting through it
                        # corrupts layer Y.  Preserve the recorded raw point.
                        incompatible_layout = True
                        continue
                    # The stored x/y are normalised INSIDE THE ANALYSIS BOX - the very frame the marker Y,
                    # the layer bands and the tolerances live in.  A recorded point is therefore already
                    # valid as long as the analysis box and the diamond size (a real minimap zoom) have
                    # not changed; the CANVAS sub-region must not decide it.  On the operator's profile
                    # (13:37) layer1's three points were recorded with canvas (0, 82), layer2's left/rope
                    # with (21, 61) and layer2's RIGHT point with (0, 82) again - one recording, three
                    # frames, because the canvas detection flips inside the same minimap box.  Projecting
                    # each point through the live canvas frame moved points that were perfectly correct:
                    # layer1's stance went from 0.676829 to 0.804878 and layer2's points to
                    # 0.591463/0.719512, so a marker standing on layer1 matched LAYER2 and layer2's band
                    # was drawn 12 px tall instead of a line.
                    try:
                        same_analysis_frame = (
                            abs(float(recorded_layout["analysis_width"])
                                - layout.analysis_width) <= 1.0
                            and abs(float(recorded_layout["analysis_height"])
                                    - layout.analysis_height) <= 1.0
                            and _diamond_geometry_matches(recorded_layout, layout)
                        )
                    except (KeyError, TypeError, ValueError):
                        same_analysis_frame = False
                try:
                    if same_analysis_frame:
                        x, y = float(point["x"]), float(point["y"])
                    else:
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
            if projected_y and not incompatible_layout \
                    and len(projected_y) == adaptive_points:
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
            if self._enabled:
                raise ValueError("请先停止巡逻，再添加楼层")
            layers = self._profile.setdefault("layers", {})
            numeric_layers = [
                int(match.group(1))
                for name in layers
                if (match := re.search(r"(\d+)$", name)) is not None
            ]
            # Capture the effective range before adding the new top floor.
            # A legacy/empty explicit range still resolves to the current
            # bottom floor here, which is the correct start to preserve.
            old_start, _old_end = self.patrol_range_locked()
            next_number = max(numeric_layers, default=0) + 1
            next_name = f"layer{next_number}"
            # Assignment is intentionally unconditional because next_number is
            # above every existing numeric layer and therefore cannot replace
            # a calibrated row.
            layers[next_name] = {
                "y_tolerance": 0.020000,
                "calibration_status": "awaiting_left_rope_right",
            }
            # A new top floor becomes the patrol-range end immediately.  It
            # is routed now, so Start remains safely disabled until that new
            # floor has at least one recorded action point.
            route = self._profile.setdefault("route_order", [])
            if next_name not in route:
                route.append(next_name)
            # Adding from zero layers is a new patrol range, not an implicit
            # fallback: persist layer1 as its explicit start so Ctrl+Home and
            # the UI comboboxes have a real selection to operate on.  On an
            # existing map preserve the chosen lower boundary, while every
            # new highest floor becomes the patrol end.
            self._profile["patrol_start_layer"] = (
                next_name if not numeric_layers else old_start
            )
            self._profile["patrol_end_layer"] = next_name
            self._selected_layer = next_name
            self._enabled = False
            self._persist_locked()
            return next_name

    def remove_highest_layer(self) -> str:
        """Remove the highest numeric layer and select the new top layer."""

        with self._lock:
            if self._enabled:
                raise ValueError("请先停止巡逻，再删除楼层")
            layers = self._profile.setdefault("layers", {})
            numeric_layers = sorted(
                (
                    (int(match.group(1)), name)
                    for name in layers
                    if (match := re.search(r"(\d+)$", name)) is not None
                ),
                key=lambda item: item[0],
            )
            if not numeric_layers:
                raise ValueError("没有可删除的楼层")

            old_start, _old_end = self.patrol_range_locked()
            _, highest_name = numeric_layers[-1]
            del layers[highest_name]
            remaining_names = [name for _, name in numeric_layers[:-1]]
            route = [
                name for name in self._profile.get("route_order", [])
                if name in layers
            ]
            route.extend(name for name in remaining_names if name not in route)
            self._profile["route_order"] = route

            self._enabled = False
            if remaining_names:
                self._selected_layer = remaining_names[-1]
                # Top-layer additions always extend the patrol end, so
                # deleting that layer must bring the end back to the new
                # highest floor.  Preserve the start where possible, clamping
                # it to the end.
                new_end = remaining_names[-1]
                new_start = old_start if old_start in layers else new_end
                if _layer_number(new_start) > _layer_number(new_end):
                    new_start = new_end
            else:
                # The last layer was deleted: no floors remain.  Patrol stays
                # startable - with no route the character stands still and
                # only attacks/jumps - so clear the selection and range
                # instead of keeping a stale layer1 reference.
                self._selected_layer = ""
                new_start = new_end = ""
            self._profile["patrol_start_layer"] = new_start
            self._profile["patrol_end_layer"] = new_end
            self._persist_locked()
            return highest_name

    def _persist_locked(self) -> None:
        if self.config_store is not None:
            self.config_store.write_section("recording", self._profile)
            return
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
