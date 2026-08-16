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
