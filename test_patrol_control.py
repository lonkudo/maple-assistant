from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from patrol_control import CoordinateLayout, PatrolController


def profile() -> dict:
    return {
        "patrol_enabled": False,
        "route_order": ["layer1"],
        "layers": {
            "layer1": {
                "layer_y": .700000,
                "y_tolerance": .020000,
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            },
            "layer2": {
                "layer_y": .560000,
                "y_tolerance": .020000,
                "left_most_pos": {"x": .3, "y": .56},
                "right_most_pos": {"x": .6, "y": .56},
            },
        },
    }


class PatrolControllerTests(unittest.TestCase):
    @staticmethod
    def _make_adaptive(point: dict) -> None:
        point["coordinate_v2"] = {
            "x_diamond": 0.0,
            "y_diamond": 0.0,
            "recorded_layout": {},
        }

    def test_start_and_stop_are_thread_safe_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = PatrolController(Path(directory) / "map.json", profile())
            self.assertFalse(controller.is_enabled())
            controller.set_enabled(True)
            self.assertTrue(controller.is_enabled())
            controller.set_enabled(False)
            self.assertFalse(controller.is_enabled())

    def test_ui_can_select_an_existing_layer_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = PatrolController(Path(directory) / "map.json", profile())
            controller.select_layer("layer1")
            self.assertEqual(controller.selected_layer(), "layer1")
            with self.assertRaises(ValueError):
                controller.select_layer("missing")

    def test_reset_recording_clears_route_layers_and_runtime_patrol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.json"
            data = profile()
            path.write_text(json.dumps(data), encoding="utf-8")
            controller = PatrolController(path, data)
            controller.set_enabled(True)
            controller.reset_recording()

            snapshot = controller.snapshot()
            self.assertFalse(snapshot.enabled)
            self.assertEqual(snapshot.route_order, ())
            self.assertEqual(tuple(snapshot.layers), ("layer1",))
            self.assertFalse(controller.layer_is_complete("layer1"))
            # An empty route is still startable: the worker stands still and
            # only attacks (Fixed Attack / YOLO mode).
            self.assertTrue(controller.can_start())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["route_order"], [])
            self.assertNotIn("left_most_pos", saved["layers"]["layer1"])

    def test_record_uses_only_y_to_choose_layer_and_persists_six_digits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps(profile()), encoding="utf-8")
            controller = PatrolController(path, profile())

            # X is far outside the old endpoint; layer choice still comes from Y.
            recorded = controller.record_endpoint("left_most_pos", .91234567, .7012344)

            self.assertEqual(recorded.layer, "layer1")
            self.assertEqual(recorded.x, .912346)
            self.assertEqual(recorded.y, .701234)
            saved = json.loads(path.read_text(encoding="utf-8"))
            endpoint = saved["layers"]["layer1"]["left_most_pos"]
            self.assertEqual(endpoint["x"], .912346)
            self.assertEqual(endpoint["source"], "manual-ui")
            # Inactive layer calibration remains untouched.
            self.assertEqual(saved["layers"]["layer2"]["left_most_pos"]["x"], .3)

    def test_clear_endpoint_removes_point_and_updates_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["route_order"] = ["layer1"]
            path.write_text(json.dumps(data), encoding="utf-8")
            controller = PatrolController(path, data)

            self.assertIsNotNone(controller.endpoint("layer1", "left_most_pos"))
            # 清除一个点后该点消失；层仍有 right 点，路由保留。
            self.assertTrue(controller.clear_endpoint("layer1", "left_most_pos"))
            self.assertIsNone(controller.endpoint("layer1", "left_most_pos"))
            self.assertEqual(controller.snapshot().route_order, ("layer1",))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("left_most_pos",
                             saved["layers"]["layer1"])
            # 清除该层最后一个点后：路由清空、状态回到等待录制。
            self.assertTrue(controller.clear_endpoint("layer1", "right_most_pos"))
            self.assertEqual(controller.snapshot().route_order, ())
            # 再次清除不存在的数据返回 False。
            self.assertFalse(controller.clear_endpoint("layer1", "left_most_pos"))

    def test_scroll_compensated_world_y_allows_centered_marker_on_upper_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"]["layer1"]["layer_world_y"] = 0.0
            controller = PatrolController(path, data)
            controller.select_layer("layer2")

            recorded = controller.record_endpoint(
                "left_most_pos", .3, .70, world_y=-2.0,
                tracking_confidence=.9,
            )

            self.assertEqual(recorded.layer, "layer2")
            layer = controller.snapshot().layers["layer2"]
            self.assertEqual(layer["layer_world_y"], -2.0)
            self.assertEqual(layer["left_most_pos"]["tracking_confidence"], .9)

    def test_later_endpoint_inherits_established_layer_world_y(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"]["layer1"]["layer_world_y"] = 0.0
            controller = PatrolController(path, data)
            controller.select_layer("layer2")

            controller.record_endpoint(
                "left_most_pos", .3, .56, world_y=-7.25,
                tracking_confidence=.95,
            )
            # Repeating platform graphics can alias to the lower layer while
            # walking horizontally. The explicit selected layer remains the
            # authority after its first point establishes canonical world Y.
            controller.record_endpoint(
                "right_most_pos", .7, .56, world_y=-.10,
                tracking_confidence=.90,
            )

            layer = controller.snapshot().layers["layer2"]
            right = layer["right_most_pos"]
            self.assertEqual(layer["layer_world_y"], -7.25)
            self.assertEqual(right["world_y"], -7.25)
            self.assertEqual(right["observed_world_y"], -.10)

    def test_layer_world_lookup_accepts_coherent_stair_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["route_order"] = ["layer1"]
            layer = data["layers"]["layer1"]
            layer["layer_world_y"] = 1.5
            layer["world_y_tolerance"] = .75
            layer["left_most_pos"].update({
                "x": .22, "world_y": 1.5, "observed_world_y": 1.5,
                "tracking_confidence": .5,
                "coordinate_v2": {"y_diamond": 1.5},
            })
            layer["right_most_pos"].update({
                "x": .74, "world_y": 1.5, "observed_world_y": 2.836,
                "tracking_confidence": .7,
                "coordinate_v2": {"y_diamond": 2.833},
            })
            controller = PatrolController(path, data)

            self.assertEqual(controller.layer_for_world_y(2.8), "layer1")

    def test_record_rejects_unknown_layer_y(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = PatrolController(Path(directory) / "map.json", profile())
            with self.assertRaises(ValueError):
                controller.record_endpoint("right_most_pos", .5, .63)

    def test_complete_layer_can_add_and_activate_layer_above(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            path.write_text(json.dumps(data), encoding="utf-8")
            controller = PatrolController(path, data)

            controller.record_endpoint("rope_pos", .49, .70)
            self.assertTrue(controller.layer_is_complete("layer1"))
            controller.set_enabled(True)
            self.assertEqual(controller.add_layer_above(), "layer3")
            self.assertFalse(controller.is_enabled())
            self.assertEqual(controller.selected_layer(), "layer3")
            self.assertFalse(controller.layer_is_complete("layer3"))

            # The new highest layer cannot record a rope and needs only edges.
            with self.assertRaisesRegex(ValueError, "final layer"):
                controller.record_endpoint("rope_pos", .52, .42)
            snapshot = controller.snapshot()
            self.assertEqual(snapshot.route_order, ("layer1",))
            self.assertEqual(snapshot.layers["layer1"]["rope_pos"]["x"], .49)
            self.assertNotIn("rope_pos", snapshot.layers["layer3"])

    def test_incomplete_layer_can_add_another_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            data["layers"] = {"layer1": {"y_tolerance": .02}}
            data["route_order"] = []
            controller = PatrolController(Path(directory) / "map.json", data)

            self.assertEqual(controller.add_layer_above(), "layer2")
            self.assertEqual(tuple(controller.snapshot().layers), ("layer1", "layer2"))
            # Empty route = stand-still + attack mode, so it is startable.
            self.assertTrue(controller.can_start())

    def test_new_layer_first_point_records_its_y(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"]["layer1"]["rope_pos"] = {"x": .5, "y": .7}
            data["layers"].pop("layer2")
            controller = PatrolController(path, data)
            self.assertEqual(controller.add_layer_above(), "layer2")
            controller.record_endpoint("left_most_pos", .25, .501234)
            self.assertEqual(
                controller.snapshot().layers["layer2"]["layer_y"], .501234
            )

    def test_layer_y_is_average_of_recorded_points(self) -> None:
        # Layer Y must be the AVERAGE of the recorded points.  A median of
        # two points degenerates to the larger one, biasing the arrival band
        # toward one edge of the platform and making climb arrival detection
        # miss (the character lands on the platform between the points).
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"].pop("layer2")
            data["route_order"] = []
            data["layers"]["layer1"] = {"y_tolerance": .020000}
            path.write_text(json.dumps(data), encoding="utf-8")
            controller = PatrolController(path, data)
            controller.record_endpoint("left_most_pos", .2, .40)
            controller.record_endpoint("right_most_pos", .8, .42)
            self.assertAlmostEqual(
                controller.snapshot().layers["layer1"]["layer_y"], .41, places=6
            )

    def test_new_layer_records_point_even_when_world_y_order_is_off(self) -> None:
        # 层序检查只警告不拒绝：世界 Y 排序异常不再阻止录制（用户可能
        # 因地图/分辨率原因需要录到"看起来不在下层之上"的位置）。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"]["layer1"]["rope_pos"] = {"x": .5, "y": .7}
            data["layers"].pop("layer2")
            controller = PatrolController(path, data)
            controller.add_layer_above()
            recorded = controller.record_endpoint("left_most_pos", .25, .695)
            self.assertEqual(recorded.layer, "layer2")
            self.assertIsNotNone(
                controller.endpoint("layer2", "left_most_pos")
            )

    def test_recorded_points_transform_across_width_and_diamond_size(self) -> None:
        recorded_layout = CoordinateLayout(
            analysis_width=200, analysis_height=160,
            canvas_left=10, canvas_top=10, canvas_width=180, canvas_height=140,
            diamond_width=4, diamond_height=4,
        )
        current_layout = CoordinateLayout(
            analysis_width=300, analysis_height=240,
            canvas_left=30, canvas_top=20, canvas_width=240, canvas_height=200,
            diamond_width=8, diamond_height=8,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"].pop("layer2")
            path.write_text(json.dumps(data), encoding="utf-8")
            controller = PatrolController(path, data)
            controller.record_endpoint("left_most_pos", .25, .50, recorded_layout)
            controller.record_endpoint("right_most_pos", .75, .50, recorded_layout)

            mapped = controller.snapshot(current_layout).layers["layer1"]
            # Raw .25/.50/.75 ratios are intentionally not reused. Projection
            # accounts for the wider canvas and the doubled diamond size.
            self.assertAlmostEqual(mapped["left_most_pos"]["x"], 1 / 6, places=5)
            self.assertAlmostEqual(mapped["right_most_pos"]["x"], 5 / 6, places=5)
            self.assertTrue(controller.layer_is_adaptive("layer1"))
            self.assertTrue(controller.can_start())

    def test_search_crop_layout_is_not_used_to_reproject_layer_y(self) -> None:
        recorded_layout = CoordinateLayout(
            analysis_width=238, analysis_height=207,
            canvas_left=0, canvas_top=0, canvas_width=238, canvas_height=207,
            diamond_width=8, diamond_height=8,
        )
        actual_minimap_layout = CoordinateLayout(
            analysis_width=95, analysis_height=130,
            canvas_left=0, canvas_top=20, canvas_width=95, canvas_height=110,
            diamond_width=8, diamond_height=8,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            data = profile()
            data["layers"].pop("layer2")
            data["layers"]["layer1"]["layer_y"] = .437198
            controller = PatrolController(path, data)
            controller.record_endpoint(
                "left_most_pos", .20, .437198, recorded_layout
            )
            controller.record_endpoint(
                "right_most_pos", .70, .437198, recorded_layout
            )

            mapped = controller.snapshot(actual_minimap_layout).layers["layer1"]
            self.assertEqual(mapped["layer_y"], .437198)
            self.assertEqual(mapped["left_most_pos"]["y"], .437198)

    def test_legacy_ratio_only_layer_starts_but_is_not_adaptive(self) -> None:
        # Left/Rope/Right start patrol once recorded; a legacy ratio-only
        # layer starts too but is flagged non-adaptive (re-record for zoom).
        with tempfile.TemporaryDirectory() as directory:
            controller = PatrolController(Path(directory) / "map.json", profile())
            self.assertTrue(controller.can_start())
            self.assertFalse(controller.layer_is_adaptive("layer1"))

    def test_final_layer_can_start_without_rope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            data["layers"].pop("layer2")
            data["route_order"] = ["layer1"]
            for point_name in ("left_most_pos", "right_most_pos"):
                self._make_adaptive(data["layers"]["layer1"][point_name])
            controller = PatrolController(Path(directory) / "map.json", data)

            self.assertTrue(controller.layer_is_complete("layer1"))
            self.assertTrue(controller.layer_is_patrol_ready("layer1"))
            self.assertTrue(controller.can_start())

    def test_saved_final_rope_is_ignored_until_a_new_layer_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            data["route_order"] = ["layer1", "layer2"]
            data["layers"]["layer2"]["rope_pos"] = {"x": .51, "y": .56}
            controller = PatrolController(Path(directory) / "map.json", data)

            self.assertIsNone(controller.endpoint("layer2", "rope_pos"))
            self.assertNotIn("rope_pos", controller.snapshot().layers["layer2"])

            self.assertEqual(controller.add_layer_above(), "layer3")
            restored = controller.endpoint("layer2", "rope_pos")
            self.assertIsNotNone(restored)
            self.assertAlmostEqual(restored.x, .51)

    def test_non_final_layer_does_not_require_rope(self) -> None:
        # Left/Rope/Right are independent: a non-final layer with only
        # adaptive Left/Right patrols just that floor and is startable.
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            data["route_order"] = ["layer1", "layer2"]
            for layer_name in data["route_order"]:
                for point_name in ("left_most_pos", "right_most_pos"):
                    self._make_adaptive(data["layers"][layer_name][point_name])
            controller = PatrolController(Path(directory) / "map.json", data)
            self.assertTrue(controller.can_start())

    def test_rope_only_layer_is_startable(self) -> None:
        # Recording only a rope means the worker goes straight to the rope.
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            data["route_order"] = ["layer1"]
            data["layers"]["layer1"] = {"rope_pos": {"x": .5, "y": .7}}
            self._make_adaptive(data["layers"]["layer1"]["rope_pos"])
            controller = PatrolController(Path(directory) / "map.json", data)
            self.assertTrue(controller.can_start())
            self.assertTrue(controller.layer_is_patrol_ready("layer1"))

    def test_snapshot_route_order_is_bottom_up_ignoring_recording_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = profile()
            # Recorded top-first (layer2 before layer1): the snapshot must
            # still report the route bottom-up (layer1, layer2).
            data["route_order"] = ["layer2", "layer1"]
            data["layers"] = {
                "layer1": data["layers"]["layer1"],
                "layer2": data["layers"]["layer2"],
            }
            controller = PatrolController(Path(directory) / "map.json", data)
            self.assertEqual(
                controller.snapshot().route_order, ("layer1", "layer2")
            )


if __name__ == "__main__":
    unittest.main()
