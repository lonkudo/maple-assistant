import logging
import unittest

from ui_worker import (
    UiLogHandler,
    UiWorker,
    layer_display_order,
    patrol_button_states,
    record_button_is_locked,
    rope_unavailable_hint,
    tooltip_cursor_top_right_position,
)


class UiLogHandlerTests(unittest.TestCase):
    def test_higher_layers_are_displayed_above_lower_layers(self) -> None:
        self.assertEqual(
            layer_display_order(["layer1", "layer2", "layer3"]),
            ("layer3", "layer2", "layer1"),
        )

    def test_final_rope_hover_hint_tells_user_how_to_enable_it(self) -> None:
        self.assertEqual(
            rope_unavailable_hint(),
            "Add a layer to enable Rope recording.",
        )

    def test_dynamic_record_button_locks_from_saved_endpoint(self) -> None:
        endpoint = object()
        self.assertTrue(record_button_is_locked(endpoint, False))
        self.assertFalse(record_button_is_locked(endpoint, True))
        self.assertFalse(record_button_is_locked(None, False))

    def test_tooltip_is_at_cursor_top_right(self) -> None:
        self.assertEqual(
            tooltip_cursor_top_right_position(100, 200, 80, 30, (0, 0, 1920, 1080)),
            (114, 160),
        )

    def test_tooltip_stays_on_cursor_monitor_with_virtual_coordinates(self) -> None:
        self.assertEqual(
            tooltip_cursor_top_right_position(
                -100, 300, 200, 30, (-1920, 0, 0, 1080)
            ),
            (-204, 260),
        )

    def test_started_patrol_activates_stop_and_disables_start(self) -> None:
        self.assertEqual(patrol_button_states(False, True), ("normal", "disabled"))
        self.assertEqual(patrol_button_states(True, True), ("disabled", "normal"))

    def test_log_queue_drops_oldest_messages_at_capacity(self) -> None:
        handler = UiLogHandler(capacity=20)
        handler.setFormatter(logging.Formatter("%(message)s"))
        for index in range(25):
            handler.emit(logging.LogRecord(
                "test", logging.INFO, __file__, 1, f"message-{index}", (), None
            ))

        messages = []
        while not handler.messages.empty():
            messages.append(handler.messages.get_nowait())
        self.assertEqual(len(messages), 20)
        self.assertEqual(messages[0], "message-5")
        self.assertEqual(messages[-1], "message-24")

    def test_embedded_lock_click_unlocks_then_next_click_records(self) -> None:
        class Controller:
            selected = "layer1"
            endpoints = {("layer1", "left_most_pos"): object()}

            @staticmethod
            def selected_layer() -> str:
                return Controller.selected

            @staticmethod
            def select_layer(layer_name: str) -> None:
                Controller.selected = layer_name

            @staticmethod
            def endpoint(layer_name: str, point_name: str):
                return Controller.endpoints.get((layer_name, point_name))

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        worker = UiWorker.__new__(UiWorker)
        worker.patrol_controller = Controller()
        worker._unlocked_points = set()
        worker._control_status = Label()
        worker._refresh_patrol_controls = lambda: None
        recorded = []
        worker._record_endpoint = recorded.append

        worker._record_or_unlock("layer1", "left_most_pos")
        self.assertIn(("layer1", "left_most_pos"), worker._unlocked_points)
        self.assertEqual(recorded, [])
        self.assertIn("same Record button again", worker._control_status.text)

        worker._record_or_unlock("layer1", "left_most_pos")
        self.assertEqual(recorded, ["left_most_pos"])

    def test_stale_lock_without_saved_endpoint_records_on_same_click(self) -> None:
        class Controller:
            @staticmethod
            def select_layer(_layer_name):
                pass

            @staticmethod
            def endpoint(_layer_name, _point_name):
                return None

        worker = UiWorker.__new__(UiWorker)
        worker.patrol_controller = Controller()
        worker._unlocked_points = {("layer2", "left_most_pos")}
        recorded = []
        worker._record_endpoint = recorded.append

        worker._record_or_unlock("layer2", "left_most_pos")

        self.assertEqual(recorded, ["left_most_pos"])
        self.assertNotIn(("layer2", "left_most_pos"), worker._unlocked_points)

    def test_monster_apply_bounds_updates_detector_config(self) -> None:
        from monster_detector import MonsterDetector

        detector = MonsterDetector()
        worker = UiWorker.__new__(UiWorker)
        worker.monster_detector = detector
        worker._monster_apply_bounds(0, (170, 80, 80), (10, 255, 255))
        self.assertEqual(detector.configs[0].hsv_lower, (170, 80, 80))
        self.assertEqual(detector.configs[0].hsv_upper, (10, 255, 255))
        self.assertEqual(len(detector.configs), 1)

    def test_monster_apply_bounds_second_slot_adds_band(self) -> None:
        from monster_detector import MonsterDetector

        detector = MonsterDetector()
        worker = UiWorker.__new__(UiWorker)
        worker.monster_detector = detector
        worker._monster_apply_bounds(1, (100, 100, 100), (130, 255, 255))
        self.assertEqual(len(detector.configs), 2)
        self.assertEqual(detector.configs[1].hsv_lower, (100, 100, 100))
        # Slot 0 keeps its original band.
        self.assertEqual(len(detector.configs[0].hsv_lower), 3)

    def test_monster_apply_bounds_keeps_search_zone(self) -> None:
        from monster_detector import DEFAULT_MONSTER_ZONE, MonsterDetector

        detector = MonsterDetector()
        worker = UiWorker.__new__(UiWorker)
        worker.monster_detector = detector
        worker._monster_apply_bounds(0, (0, 100, 100), (30, 255, 255))
        self.assertEqual(detector.configs[0].search_zone, DEFAULT_MONSTER_ZONE)

    def test_monster_ingest_saves_applies_and_previews(self) -> None:
        import tempfile
        from pathlib import Path

        from PIL import Image
        from monster_profiles import MonsterProfileStore

        class Detector:
            def __init__(self) -> None:
                self.configs = [None]

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        class Box:
            def __init__(self) -> None:
                self.image = None

            def delete(self, _tag):
                pass

            def create_image(self, *args, **kwargs):
                self.image = kwargs.get("image")

        with tempfile.TemporaryDirectory() as directory:
            store = MonsterProfileStore(Path(directory))
            worker = UiWorker.__new__(UiWorker)
            worker.monster_profile_store = store
            worker.monster_detector = Detector()
            worker._monster_status = Label()
            worker._monster_boxes = [Box()]
            worker._monster_box_names = [""]
            worker._monster_apply_bounds = lambda slot, lower, upper, **kwargs: setattr(
                worker.monster_detector, "configs", [
                    (lower, upper) for _ in worker.monster_detector.configs
                ]
            )
            worker._monster_box_set_image = lambda slot, image: setattr(
                worker._monster_boxes[slot], "image", image
            )

            image = Image.new("RGB", (40, 40), (200, 160, 40))
            worker._monster_ingest(image, "bee", 0)

            self.assertIsNotNone(worker.monster_detector.configs[0])
            self.assertIsNotNone(worker._monster_boxes[0].image)
            self.assertEqual(worker._monster_box_names[0], "bee")
            self.assertIn("bee", worker._monster_status.text)
            self.assertEqual(store.names(), ["bee"])

    def test_monster_paste_uses_first_empty_slot(self) -> None:
        worker = UiWorker.__new__(UiWorker)
        worker._monster_box_names = ["bee", "", ""]
        self.assertEqual(worker._monster_first_empty_slot(), 1)
        worker._monster_box_names = ["a", "b", "c"]
        self.assertEqual(worker._monster_first_empty_slot(), 0)

    def test_monster_ingest_without_store_reports_unavailable(self) -> None:
        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        from PIL import Image

        worker = UiWorker.__new__(UiWorker)
        worker.monster_profile_store = None
        worker._monster_status = Label()
        worker._monster_ingest(Image.new("RGB", (10, 10)), "bee", 0)
        self.assertIn("unavailable", worker._monster_status.text)

    def test_yolo_start_launches_subprocess_and_stop_terminates(self) -> None:
        import subprocess
        from unittest import mock

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        class Button:
            def __init__(self) -> None:
                self.state = "normal"

            def configure(self, *, state: str) -> None:
                self.state = state

        worker = UiWorker.__new__(UiWorker)
        worker._yolo_process = None
        worker._yolo_threshold_var = type(
            "Var", (), {"get": lambda self: "0.33", "set": lambda self, v: None}
        )()
        worker._yolo_show_var = type("Var", (), {"get": lambda self: False})()
        worker._yolo_attack_range_var = type("Var", (), {"get": lambda self: 800})()
        worker._yolo_zone_w_var = type("Var", (), {"get": lambda self: 60})()
        worker._yolo_zone_h_var = type("Var", (), {"get": lambda self: 60})()
        worker._yolo_zone_shift_y_var = type("Var", (), {"get": lambda self: 0})()
        worker._yolo_status = Label()
        worker._yolo_run_button = Button()
        worker._yolo_stop_button = Button()

        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None  # running
        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen:
            worker._yolo_start()
            args = popen.call_args[0][0]
            self.assertIn("--threshold", args)
            self.assertIn("0.33", args)
            self.assertIn("--no-show", args)  # show toggle off by default
            self.assertIn("--attack-range", args)
            self.assertIn("800", args)
            self.assertIn("--zone-width", args)
            self.assertIn("0.60", args)
            self.assertIn("--zone-height", args)
            self.assertIn("0.60", args)
            self.assertIn("--zone-shift-y", args)
            self.assertIn("0.00", args)
        self.assertEqual(worker._yolo_run_button.state, "disabled")
        self.assertEqual(worker._yolo_stop_button.state, "normal")
        self.assertIn("running", worker._yolo_status.text)

        fake_proc.poll.return_value = 0  # exited
        worker._yolo_stop()
        self.assertEqual(worker._yolo_run_button.state, "normal")
        self.assertEqual(worker._yolo_stop_button.state, "disabled")
        self.assertIn("stopped", worker._yolo_status.text)

    def test_yolo_show_toggle_adds_visible_mode(self) -> None:
        from unittest import mock

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        class Button:
            def __init__(self) -> None:
                self.state = "normal"

            def configure(self, *, state: str = None, style: str = None) -> None:
                if state is not None:
                    self.state = state

        worker = UiWorker.__new__(UiWorker)
        worker._yolo_process = None
        worker._yolo_threshold_var = type(
            "Var", (), {"get": lambda self: "0.4", "set": lambda self, v: None}
        )()
        worker._yolo_show_var = type("Var", (), {"get": lambda self: True})()
        worker._yolo_attack_range_var = type("Var", (), {"get": lambda self: 1200})()
        worker._yolo_zone_w_var = type("Var", (), {"get": lambda self: 80})()
        worker._yolo_zone_h_var = type("Var", (), {"get": lambda self: 50})()
        worker._yolo_zone_shift_y_var = type("Var", (), {"get": lambda self: 20})()
        worker._yolo_status = Label()
        worker._yolo_run_button = Button()
        worker._yolo_stop_button = Button()

        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen:
            worker._yolo_start()
            args = popen.call_args[0][0]
            self.assertNotIn("--no-show", args)  # show enabled -> window mode
            self.assertIn("--attack-range", args)
            self.assertIn("1200", args)
            self.assertIn("--zone-width", args)
            self.assertIn("0.80", args)
            self.assertIn("--zone-height", args)
            self.assertIn("0.50", args)
            self.assertIn("--zone-shift-y", args)
            self.assertIn("0.20", args)

    def test_monster_motion_activates_without_picture(self) -> None:
        from monster_detector import MonsterDetector

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        detector = MonsterDetector()
        worker = UiWorker.__new__(UiWorker)
        worker.monster_detector = detector
        worker._monster_status = Label()
        worker._monster_method_var = type(
            "Var", (), {"get": lambda self: "motion"}
        )()

        worker._monster_on_method_change()

        self.assertEqual(detector.configs[0].method, "motion")
        self.assertTrue(detector.configs[0].enabled)
        self.assertIn("motion", worker._monster_status.text.lower())

    def test_monster_clear_slot_disables_band_and_deletes_profile(self) -> None:
        import tempfile
        from pathlib import Path

        from PIL import Image
        from monster_detector import DEFAULT_MONSTER_ZONE, MonsterDetector
        from monster_profiles import MonsterProfileStore

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        with tempfile.TemporaryDirectory() as directory:
            store = MonsterProfileStore(Path(directory))
            detector = MonsterDetector()
            worker = UiWorker.__new__(UiWorker)
            worker.monster_profile_store = store
            worker.monster_detector = detector
            worker._monster_box_names = ["bee", "", ""]
            worker._monster_boxes = [None, None, None]
            worker._monster_box_photos = [None, None, None]
            worker._monster_clear_buttons = [None, None, None]
            worker._monster_status = Label()
            worker._draw_monster_box_placeholder = lambda slot: None

            store.save("bee", Image.new("RGB", (10, 10), (200, 160, 40)))
            worker._monster_apply_bounds(0, (0, 100, 100), (30, 255, 255))
            self.assertTrue(detector.configs[0].enabled)

            worker._monster_clear_slot(0)

            self.assertFalse(detector.configs[0].enabled)
            self.assertEqual(worker._monster_box_names[0], "")
            self.assertEqual(store.names(), [])
            self.assertIn("Removed", worker._monster_status.text)

    def test_reset_has_no_confirmation_and_stops_before_clearing(self) -> None:
        calls = []

        class Controller:
            def set_enabled(self, enabled):
                calls.append(("enabled", enabled))

            def reset_recording(self):
                calls.append(("reset",))

            @staticmethod
            def map_name() -> str:
                return ""

        class Label:
            def configure(self, **_kwargs):
                pass

        worker = UiWorker.__new__(UiWorker)
        worker.patrol_controller = Controller()
        worker.on_patrol_stop = lambda: calls.append(("stop_input",))
        worker._unlocked_points = {("layer1", "left_most_pos")}
        worker._layer_row_names = ("layer1",)
        worker._refresh_patrol_controls = lambda: calls.append(("refresh",))
        worker._control_status = Label()

        worker._reset_recording()

        self.assertEqual(calls[:3], [
            ("enabled", False), ("stop_input",), ("reset",)
        ])
        self.assertEqual(worker._unlocked_points, set())


if __name__ == "__main__":
    unittest.main()
