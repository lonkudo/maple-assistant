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

    def test_display_order_ignores_recording_order(self) -> None:
        # The top layer was recorded before the lower one (Add Layer
        # auto-selects the new layer): layer2 must still display ABOVE
        # layer1 - never "layer1 on top of layer2".
        self.assertEqual(
            layer_display_order(["layer2", "layer1"]),
            ("layer2", "layer1"),
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
        worker._yolo_fps_var = type("Var", (), {"get": lambda self: 15})()
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
            self.assertIn("--fps", args)
            self.assertIn("15", args)
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

    def test_yolo_settings_save_and_load_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

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

        with tempfile.TemporaryDirectory() as directory:
            worker = UiWorker.__new__(UiWorker)
            worker._yolo_process = None
            worker._yolo_threshold_var = Var("0.35")
            worker._yolo_attack_range_var = Var(900)
            worker._yolo_zone_w_var = Var(70)
            worker._yolo_zone_h_var = Var(55)
            worker._yolo_zone_shift_y_var = Var(15)
            worker._yolo_show_var = Var(True)
            worker._yolo_attack_var = Var(True)
            worker._yolo_attack_key_var = Var("alt")
            worker._yolo_status = Label()
            worker._yolo_run_button = Button()
            worker._yolo_stop_button = Button()

            def fake_path(self):
                return Path(directory) / "yolo_detection_settings.json"

            with mock.patch.object(UiWorker, "_yolo_settings_path", fake_path):
                worker._yolo_save_settings()
                saved = Path(directory) / "yolo_detection_settings.json"
                self.assertTrue(saved.is_file())

                # A fresh worker loads the saved values back.
                loader = UiWorker.__new__(UiWorker)
                loader._yolo_process = None
                loader._yolo_threshold_var = Var("0.4")
                loader._yolo_attack_range_var = Var(800)
                loader._yolo_zone_w_var = Var(60)
                loader._yolo_zone_h_var = Var(60)
                loader._yolo_zone_shift_y_var = Var(0)
                loader._yolo_show_var = Var(False)
                loader._yolo_attack_var = Var(False)
                loader._yolo_attack_key_var = Var("ctrl")
                loader._yolo_status = Label()
                loader._yolo_run_button = Button()
                loader._yolo_stop_button = Button()
                loader._yolo_on_range_change = lambda *a: None
                loader._yolo_on_zone_change = lambda *a: None
                loader._yolo_sync_show_button = lambda: None
                with mock.patch.object(UiWorker, "_yolo_settings_path", fake_path):
                    loader._yolo_load_settings()

                self.assertEqual(loader._yolo_threshold_var.get(), 0.35)
                self.assertEqual(loader._yolo_attack_range_var.get(), 900)
                self.assertEqual(loader._yolo_zone_w_var.get(), 70)
                self.assertEqual(loader._yolo_zone_h_var.get(), 55)
                self.assertEqual(loader._yolo_zone_shift_y_var.get(), 15)
                self.assertTrue(loader._yolo_show_var.get())
                self.assertTrue(loader._yolo_attack_var.get())
                self.assertEqual(loader._yolo_attack_key_var.get(), "alt")

    def test_yolo_save_config_confirms_and_persists(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        with tempfile.TemporaryDirectory() as directory:
            worker = UiWorker.__new__(UiWorker)
            worker._yolo_threshold_var = Var("0.42")
            worker._yolo_attack_range_var = Var(1000)
            worker._yolo_zone_w_var = Var(60)
            worker._yolo_zone_h_var = Var(26)
            worker._yolo_zone_shift_y_var = Var(1)
            worker._yolo_show_var = Var(True)
            worker._yolo_attack_var = Var(True)
            worker._yolo_attack_key_var = Var("ctrl")
            worker._yolo_status = Label()

            def fake_path(self):
                return Path(directory) / "yolo_detection_settings.json"

            with mock.patch.object(UiWorker, "_yolo_settings_path", fake_path):
                worker._yolo_save_config()
                saved = Path(directory) / "yolo_detection_settings.json"
                self.assertTrue(saved.is_file())
                import json

                data = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(data["threshold"], 0.42)
                self.assertEqual(data["attack_range"], 1000)
                self.assertTrue(data["auto_attack"])
                self.assertEqual(data["attack_key"], "ctrl")
                self.assertIn("Configuration saved", worker._yolo_status.text)

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
