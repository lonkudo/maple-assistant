import logging
import unittest

from ui_worker import (
    UiLogHandler,
    UiWorker,
    bindable_keys_hint,
    keysym_to_scan_key,
    layer_display_order,
    patrol_button_states,
    record_button_is_locked,
    recorded_coordinate_text,
    machine_name_button_text,
    normalize_quick_messages,
    rope_unavailable_hint,
    tooltip_cursor_top_right_position,
    _clamp_window_geometry,
    _load_window_geometry,
    _parse_window_geometry,
    _save_window_geometry,
)


class UiLogHandlerTests(unittest.TestCase):
    def test_yolo_panel_is_temporarily_hidden_behind_restore_flag(self) -> None:
        self.assertFalse(UiWorker._SHOW_YOLO_PANEL)

    def test_machine_name_button_uses_edit_hint_only_when_empty(self) -> None:
        self.assertEqual(machine_name_button_text(""), "修改名称")
        self.assertEqual(machine_name_button_text("  电脑A  "), "电脑A")

    def test_quick_messages_are_normalized_and_bounded(self) -> None:
        self.assertEqual(
            normalize_quick_messages(["  hello ", "", None, "world"]),
            ["hello", "world"],
        )
        self.assertEqual(normalize_quick_messages(["a", "b"], 1), ["a"])

    def test_quick_message_short_click_copies_to_clipboard(self) -> None:
        class Root:
            copied = ""
            def clipboard_clear(self): self.copied = ""
            def clipboard_append(self, value): self.copied += value
            def update_idletasks(self): pass

        class Label:
            text = ""
            def configure(self, **kwargs): self.text = kwargs.get("text", "")

        worker = UiWorker.__new__(UiWorker)
        worker._root = Root()
        worker._quick_messages = ["回城补给"]
        worker._quick_message_press_job = None
        worker._quick_message_hold_fired = False
        worker._quick_message_status = Label()
        worker._quick_message_release(0)
        self.assertEqual(worker._root.copied, "回城补给")
        self.assertIn("已复制", worker._quick_message_status.text)

    def test_quick_delete_requires_hold_callback(self) -> None:
        class Label:
            text = ""
            def configure(self, **kwargs): self.text = kwargs.get("text", "")

        worker = UiWorker.__new__(UiWorker)
        worker._quick_messages = ["A", "B"]
        worker._quick_delete_press_job = object()
        worker._quick_delete_hold_fired = False
        worker._quick_message_status = Label()
        worker._quick_messages_save = lambda: None
        worker._render_quick_messages = lambda *args: None
        worker._quick_delete(0)
        self.assertEqual(worker._quick_messages, ["B"])
        self.assertTrue(worker._quick_delete_hold_fired)

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

    def test_keysym_to_scan_key_limits_to_bindable_hotkeys(self) -> None:
        # Game-usable hotkeys bind: modifiers, nav/edit, space, 1-9.
        self.assertEqual(keysym_to_scan_key("1"), "1")
        self.assertEqual(keysym_to_scan_key("9"), "9")
        self.assertEqual(keysym_to_scan_key("Shift_L"), "shift")
        self.assertEqual(keysym_to_scan_key("Shift_R"), "shift")
        self.assertEqual(keysym_to_scan_key("Control_L"), "ctrl")
        # Alt is the jump key in-game: NOT bindable.
        self.assertIsNone(keysym_to_scan_key("Alt_L"))
        self.assertIsNone(keysym_to_scan_key("Alt_R"))
        self.assertEqual(keysym_to_scan_key("Delete"), "delete")
        self.assertEqual(keysym_to_scan_key("End"), "end")
        self.assertEqual(keysym_to_scan_key("Prior"), "pageup")
        self.assertEqual(keysym_to_scan_key("Next"), "pagedown")
        self.assertEqual(keysym_to_scan_key("Home"), "home")
        self.assertEqual(keysym_to_scan_key("Insert"), "insert")
        self.assertEqual(keysym_to_scan_key("space"), "space")
        # "a" is bindable (players bind buffs/skills there).
        self.assertEqual(keysym_to_scan_key("a"), "a")
        self.assertEqual(keysym_to_scan_key("A"), "a")
        # Everything else is NOT bindable (other letters, F-keys, numpad,
        # "0", punctuation, Escape): the previous binding must stay.
        self.assertIsNone(keysym_to_scan_key("q"))
        self.assertIsNone(keysym_to_scan_key("Q"))
        self.assertIsNone(keysym_to_scan_key("F4"))
        self.assertIsNone(keysym_to_scan_key("0"))
        self.assertIsNone(keysym_to_scan_key("Tab"))
        self.assertIsNone(keysym_to_scan_key("KP_7"))
        self.assertIsNone(keysym_to_scan_key("minus"))
        self.assertIsNone(keysym_to_scan_key("Escape"))
        self.assertIsNone(keysym_to_scan_key("exclam"))
        self.assertIsNone(keysym_to_scan_key(""))

    def test_final_rope_hover_hint_tells_user_how_to_enable_it(self) -> None:
        self.assertEqual(
            rope_unavailable_hint(),
            "添加上层后即可录制绳索位置。",
        )

    def test_bindable_keys_hint_lists_all_bindable_hotkeys(self) -> None:
        hint = bindable_keys_hint()
        for key in ("1", "9", "a", "space", "ctrl", "home", "insert"):
            self.assertIn(key, hint)
        # Movement/jump keys are explicitly NOT bindable and the hint says so.
        self.assertNotIn("left", hint.split("可绑定按键：", 1)[1].split("\n\n", 1)[0])
        self.assertIn("不可绑定", hint)

    def test_dynamic_record_button_locks_from_saved_endpoint(self) -> None:
        endpoint = object()
        self.assertTrue(record_button_is_locked(endpoint, False))
        self.assertFalse(record_button_is_locked(endpoint, True))
        self.assertFalse(record_button_is_locked(None, False))

    def test_recorded_coordinate_text_uses_four_display_decimals_only(self) -> None:
        self.assertEqual(
            recorded_coordinate_text(0.123456, 0.987654),
            "x=0.1235 y=0.9877",
        )

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
        # Stop is enabled only while the patrol is running; it greys out when
        # the patrol is stopped.
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

    def test_long_press_unlocks_and_clears_then_short_click_records(self) -> None:
        class Controller:
            selected = "layer1"
            endpoints = {("layer1", "left_most_pos"): object()}
            cleared = []

            @staticmethod
            def selected_layer() -> str:
                return Controller.selected

            @staticmethod
            def select_layer(layer_name: str) -> None:
                Controller.selected = layer_name

            @staticmethod
            def endpoint(layer_name: str, point_name: str):
                return Controller.endpoints.get((layer_name, point_name))

            @staticmethod
            def clear_endpoint(layer_name: str, point_name: str) -> bool:
                removed = Controller.endpoints.pop((layer_name, point_name), None)
                Controller.cleared.append((layer_name, point_name))
                return removed is not None

        class Label:
            def __init__(self) -> None:
                self.text = ""

            def configure(self, *, text: str) -> None:
                self.text = text

        worker = UiWorker.__new__(UiWorker)
        worker._root = None
        worker.patrol_controller = Controller()
        worker._unlocked_points = set()
        worker._record_press_job = None
        worker._record_hold_fired = False
        worker._control_status = Label()
        worker._refresh_patrol_controls = lambda: None
        recorded = []
        worker._record_endpoint = recorded.append

        # 短按已录制按钮：不解锁，仅提示需要长按。
        worker._record_button_press("layer1", "left_most_pos")
        worker._record_button_release("layer1", "left_most_pos")
        self.assertNotIn(("layer1", "left_most_pos"), worker._unlocked_points)
        self.assertEqual(recorded, [])
        self.assertIn("长按", worker._control_status.text)

        # 长按 1 秒：解锁并清除该点录制；释放本次长按不得录制。
        worker._record_button_press("layer1", "left_most_pos")
        worker._record_button_hold("layer1", "left_most_pos")
        worker._record_button_release("layer1", "left_most_pos")
        self.assertIn(("layer1", "left_most_pos"), worker._unlocked_points)
        self.assertEqual(Controller.cleared, [("layer1", "left_most_pos")])
        self.assertIn("已解锁并清除", worker._control_status.text)
        self.assertEqual(recorded, [])

        # 重新点击：录制新位置。
        worker._record_button_press("layer1", "left_most_pos")
        worker._record_button_release("layer1", "left_most_pos")
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
        worker._root = None
        worker.patrol_controller = Controller()
        worker._unlocked_points = {("layer2", "left_most_pos")}
        worker._record_press_job = None
        worker._record_hold_fired = False
        recorded = []
        worker._record_endpoint = recorded.append

        worker._record_button_release("layer2", "left_most_pos")

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
        worker._YOLO_MONSTER_DETECTION_ENABLED = True
        worker._yolo_process = None
        worker._yolo_threshold_var = type(
            "Var", (), {"get": lambda self: "0.33", "set": lambda self, v: None}
        )()
        worker._yolo_show_var = type("Var", (), {"get": lambda self: False})()
        worker._yolo_fps_var = type("Var", (), {"get": lambda self: 15})()
        worker._yolo_attack_range_var = type("Var", (), {"get": lambda self: 800})()
        worker._yolo_min_mob_var = type("Var", (), {"get": lambda self: 60})()
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
            self.assertIn("--min-mob-size", args)
            self.assertIn("60", args)
            self.assertIn("--zone-width", args)
            self.assertIn("0.60", args)
            self.assertIn("--zone-height", args)
            self.assertIn("0.60", args)
            self.assertIn("--zone-shift-y", args)
            self.assertIn("0.00", args)
        self.assertEqual(worker._yolo_run_button.state, "disabled")
        self.assertEqual(worker._yolo_stop_button.state, "normal")
        self.assertIn("运行中", worker._yolo_status.text)

        fake_proc.poll.return_value = 0  # exited
        worker._yolo_stop()
        self.assertEqual(worker._yolo_run_button.state, "normal")
        self.assertEqual(worker._yolo_stop_button.state, "disabled")
        self.assertIn("已停止", worker._yolo_status.text)

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
        worker._YOLO_MONSTER_DETECTION_ENABLED = True
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
            worker._yolo_attack_range_var = Var(30)
            worker._yolo_min_mob_var = Var(2)
            worker._yolo_zone_w_var = Var(70)
            worker._yolo_zone_h_var = Var(55)
            worker._yolo_zone_shift_y_var = Var(15)
            worker._yolo_show_var = Var(True)
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
                loader._yolo_min_mob_var = Var(60)
                loader._yolo_zone_w_var = Var(60)
                loader._yolo_zone_h_var = Var(60)
                loader._yolo_zone_shift_y_var = Var(0)
                loader._yolo_show_var = Var(False)
                loader._yolo_status = Label()
                loader._yolo_run_button = Button()
                loader._yolo_stop_button = Button()
                loader._yolo_on_range_change = lambda *a: None
                loader._yolo_on_min_mob_change = lambda *a: None
                loader._yolo_on_zone_change = lambda *a: None
                loader._yolo_sync_show_button = lambda: None
                with mock.patch.object(UiWorker, "_yolo_settings_path", fake_path):
                    loader._yolo_load_settings()

                self.assertEqual(loader._yolo_threshold_var.get(), 0.35)
                self.assertEqual(loader._yolo_attack_range_var.get(), 30)
                self.assertEqual(loader._yolo_min_mob_var.get(), 2)
                self.assertEqual(loader._yolo_zone_w_var.get(), 70)
                self.assertEqual(loader._yolo_zone_h_var.get(), 55)
                self.assertEqual(loader._yolo_zone_shift_y_var.get(), 15)
                self.assertTrue(loader._yolo_show_var.get())

    def test_yolo_settings_migrate_old_pixel_values_to_percent(self) -> None:
        # Pre-percent builds saved attack_range/min_mob_size in pixels (with
        # the 2561px reference width).  Loading must convert them to %.
        import json
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
            path = Path(directory) / "yolo_detection_settings.json"
            path.write_text(json.dumps({
                "threshold": 0.4,
                "attack_range": 900,     # old: 900px on a 2561-wide client
                "min_mob_size": 60,      # old: 60px
                "detection_fps": 10,
                "zone_width": 60, "zone_height": 60, "zone_shift_y": 0,
                "show_detection": False,
            }), encoding="utf-8")
            worker = UiWorker.__new__(UiWorker)
            worker._yolo_process = None
            worker._yolo_threshold_var = Var("0.4")
            worker._yolo_attack_range_var = Var(800)
            worker._yolo_min_mob_var = Var(60)
            worker._yolo_zone_w_var = Var(60)
            worker._yolo_zone_h_var = Var(60)
            worker._yolo_zone_shift_y_var = Var(0)
            worker._yolo_show_var = Var(False)
            worker._yolo_status = Label()
            worker._yolo_run_button = Button()
            worker._yolo_stop_button = Button()
            worker._yolo_on_range_change = lambda *a: None
            worker._yolo_on_min_mob_change = lambda *a: None
            worker._yolo_on_zone_change = lambda *a: None
            worker._yolo_sync_show_button = lambda: None
            with mock.patch.object(UiWorker, "_yolo_settings_path",
                                   lambda self: path):
                worker._yolo_load_settings()
            # 900px / 2561 * 100 ~= 35%; 60px / 2561 * 100 ~= 2%.
            self.assertEqual(worker._yolo_attack_range_var.get(), 35)
            self.assertEqual(worker._yolo_min_mob_var.get(), 2)

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
            worker._yolo_attack_range_var = Var(35)
            worker._yolo_zone_w_var = Var(60)
            worker._yolo_zone_h_var = Var(26)
            worker._yolo_zone_shift_y_var = Var(1)
            worker._yolo_show_var = Var(True)
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
                self.assertEqual(data["attack_range"], 35)
                # 自动攻击由攻击模式面板控制，YOLO 设置不再保存它。
                self.assertNotIn("auto_attack", data)
                self.assertNotIn("attack_key", data)
                self.assertIn("配置已保存", worker._yolo_status.text)

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

    def test_fixed_attack_mode_greys_yolo_panel_and_applies_worker(self) -> None:
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
                self.text = ""

            def configure(self, *, text: str = None, style: str = None) -> None:
                if text is not None:
                    self.text = text

        class FakeWidget:
            def __init__(self) -> None:
                self.states = []

            def state(self, args) -> None:
                self.states.append(args)

        class FakePanel:
            def __init__(self) -> None:
                self.children = [FakeWidget(), FakeWidget()]

            def winfo_children(self):
                return self.children

        class FakeAttackWorker:
            def __init__(self) -> None:
                self.enabled = False
                self.attack_interval = 3.0
                self.attack_jitter_seconds = .1
                self.attack_key = "ctrl"

            def set_key(self, key):
                self.attack_key = key
                return True

        class FakeMover:
            def __init__(self) -> None:
                self.calls = []

            def set_yolo_detection_active(self, active) -> None:
                self.calls.append(active)

        worker = UiWorker.__new__(UiWorker)
        # Exercise the preserved recovery path; production temporarily leaves
        # this flag False until a better-trained monster model is available.
        worker._YOLO_MONSTER_DETECTION_ENABLED = True
        worker.attack_worker = FakeAttackWorker()
        worker.movement_worker = FakeMover()
        worker._attack_mode_var = Var("yolo")
        worker._fixed_interval_var = Var(3.0)
        worker._fixed_random_gap_var = Var(.1)
        worker._fixed_attack_key_var = Var("ctrl")
        worker._fixed_interval_label = Label()
        worker._fixed_key_button = Button()
        worker._fixed_status = Label()
        worker._yolo_status = Label()
        worker._yolo_panel = FakePanel()

        worker._fixed_attack_key_var.set("shift")
        worker._fixed_interval_var.set(2.5)
        worker._fixed_random_gap_var.set(.4)
        worker._attack_mode_var.set("fixed")

        def fake_path(self):
            return Path(tempfile.gettempdir()) / "test_fixed_grey_settings.json"

        with mock.patch.object(UiWorker, "_fixed_settings_path", fake_path):
            worker._fixed_on_mode_change()

            # Fixed mode applied to the worker live.
            self.assertTrue(worker.attack_worker.enabled)
            self.assertEqual(worker.attack_worker.attack_interval, 2.5)
            self.assertEqual(worker.attack_worker.attack_jitter_seconds, .4)
            self.assertEqual(worker.attack_worker.attack_key, "shift")
            # YOLO panel greyed out; status line reflects the mode.
            for child in worker._yolo_panel.children:
                self.assertIn(["disabled"], child.states)
            self.assertIn("固定攻击已启用", worker._fixed_status.text)
            self.assertIn("基础 2.5s", worker._fixed_status.text)
            self.assertIn("范围 (2.5s, 2.9s)", worker._fixed_status.text)
            # The jump-rope logic switched to minimap (YOLO inactive).
            self.assertEqual(worker.movement_worker.calls, [False])

            # Switching back to YOLO restores the panel and disables the worker.
            worker._attack_mode_var.set("yolo")
            worker._fixed_on_mode_change()
            self.assertFalse(worker.attack_worker.enabled)
            for child in worker._yolo_panel.children:
                self.assertIn(["!disabled"], child.states)
            self.assertIn("未启用", worker._fixed_status.text)
            self.assertEqual(worker.movement_worker.calls, [False, True])

    def test_fixed_attack_settings_roundtrip(self) -> None:
        import json
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
                self.text = ""

            def configure(self, *, text: str = None, style: str = None) -> None:
                if text is not None:
                    self.text = text

        class FakePanel:
            def __init__(self) -> None:
                self.children = []

            def winfo_children(self):
                return self.children

        class FakeAttackWorker:
            def __init__(self) -> None:
                self.enabled = False
                self.attack_interval = 3.0
                self.attack_jitter_seconds = .1
                self.attack_key = "ctrl"

            def set_key(self, key):
                self.attack_key = key
                return True

        def make_worker() -> UiWorker:
            w = UiWorker.__new__(UiWorker)
            w.attack_worker = FakeAttackWorker()
            w._attack_mode_var = Var("yolo")
            w._fixed_interval_var = Var(3.0)
            w._fixed_random_gap_var = Var(.1)
            w._fixed_attack_key_var = Var("ctrl")
            w._fixed_interval_label = Label()
            w._fixed_key_button = Button()
            w._fixed_status = Label()
            w._yolo_status = Label()
            w._yolo_panel = FakePanel()
            return w

        with tempfile.TemporaryDirectory() as directory:
            worker = make_worker()
            worker._attack_mode_var.set("fixed")
            worker._fixed_interval_var.set(4.5)
            worker._fixed_random_gap_var.set(.6)
            worker._fixed_attack_key_var.set("delete")

            def fake_path(self):
                return Path(directory) / "fixed_attack_settings.json"

            with mock.patch.object(UiWorker, "_fixed_settings_path", fake_path):
                worker._fixed_on_change()
                saved = Path(directory) / "fixed_attack_settings.json"
                self.assertTrue(saved.is_file())
                data = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(data["attack_mode"], "fixed")
                self.assertEqual(data["interval_seconds"], 4.5)
                self.assertEqual(data["random_gap_seconds"], .6)
                self.assertEqual(data["attack_key"], "delete")

                loader = make_worker()
                with mock.patch.object(UiWorker, "_fixed_settings_path", fake_path):
                    loader._fixed_load_settings()

                self.assertEqual(loader._attack_mode_var.get(), "fixed")
                self.assertEqual(loader._fixed_interval_var.get(), 4.5)
                self.assertEqual(loader._fixed_random_gap_var.get(), .6)
                self.assertEqual(loader._fixed_attack_key_var.get(), "delete")
                self.assertTrue(loader.attack_worker.enabled)
                self.assertEqual(loader.attack_worker.attack_interval, 4.5)
                self.assertEqual(loader.attack_worker.attack_jitter_seconds, .6)
                self.assertEqual(loader.attack_worker.attack_key, "delete")

    def test_disabled_yolo_setting_is_coerced_to_fixed_attack(self) -> None:
        class Var:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value

        class Label:
            def configure(self, **_kwargs): pass

        class AttackWorker:
            enabled = False
            attack_interval = 3.0
            attack_key = "ctrl"
            def set_key(self, key):
                self.attack_key = key
                return True

        worker = UiWorker.__new__(UiWorker)
        worker._attack_mode_var = Var("yolo")
        worker._fixed_interval_var = Var(3.0)
        worker._fixed_attack_key_var = Var("ctrl")
        worker._fixed_interval_label = Label()
        worker._fixed_status = Label()
        worker._yolo_status = Label()
        worker._yolo_panel = None
        worker.attack_worker = AttackWorker()
        worker.movement_worker = None
        worker._fixed_save_settings = lambda _data: None

        worker._fixed_on_change()

        self.assertEqual(worker._attack_mode_var.get(), "fixed")
        self.assertTrue(worker.attack_worker.enabled)

    def test_random_gap_buttons_adjust_in_point_one_second_steps(self) -> None:
        class Var:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value

        worker = UiWorker.__new__(UiWorker)
        worker._fixed_random_gap_var = Var(.1)
        changes = []
        worker._fixed_on_change = lambda: changes.append(
            worker._fixed_random_gap_var.get()
        )

        worker._fixed_adjust_random_gap(.1)
        worker._fixed_adjust_random_gap(-.1)
        worker._fixed_adjust_random_gap(-.1)

        self.assertEqual(changes, [.2, .1, 0.0])

    def test_shutdown_panel_roundtrip_and_worker_apply(self) -> None:
        import json
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

        class Slider:
            def __init__(self) -> None:
                self.states = []

            def state(self, args) -> None:
                self.states.append(args)

        class FakeShutdownWorker:
            def __init__(self) -> None:
                self.enabled = False
                self.hours = 3.0
                self._deadline = None

            def set_hours(self, hours) -> None:
                self.hours = hours
                self._deadline = None

        def make_worker() -> UiWorker:
            w = UiWorker.__new__(UiWorker)
            w.shutdown_worker = FakeShutdownWorker()
            w._shutdown_enabled_var = Var(False)
            w._shutdown_hours_var = Var(3.0)
            w._shutdown_hours_label = Label()
            w._shutdown_slider = Slider()
            w._shutdown_status = Label()
            return w

        with tempfile.TemporaryDirectory() as directory:
            worker = make_worker()
            worker._shutdown_enabled_var.set(True)
            worker._shutdown_hours_var.set(2.5)

            def fake_path(self):
                return Path(directory) / "additional_functions_settings.json"

            with mock.patch.object(UiWorker, "_shutdown_settings_path",
                                   fake_path):
                worker._shutdown_on_change()
                saved = Path(directory) / "additional_functions_settings.json"
                self.assertTrue(saved.is_file())
                data = json.loads(saved.read_text(encoding="utf-8"))
                self.assertTrue(data["shutdown_enabled"])
                self.assertEqual(data["shutdown_hours"], 2.5)
                # Applied to the worker live + slider enabled + label.
                self.assertTrue(worker.shutdown_worker.enabled)
                self.assertEqual(worker.shutdown_worker.hours, 2.5)
                self.assertIn("定时关闭已启动", worker._shutdown_status.text)
                self.assertIn(["!disabled"], worker._shutdown_slider.states)

                loader = make_worker()
                with mock.patch.object(UiWorker, "_shutdown_settings_path",
                                       fake_path):
                    loader._shutdown_load_settings()
                # 定时关闭勾选状态不跨会话恢复：启动后始终为未启用（防止
                # 上次的倒计时静默到期突然 Alt+F4）；只恢复小时数。
                self.assertFalse(loader._shutdown_enabled_var.get())
                self.assertEqual(loader._shutdown_hours_var.get(), 2.5)
                self.assertFalse(loader.shutdown_worker.enabled)
                self.assertEqual(loader.shutdown_worker.hours, 2.5)

                # Disabling greys the slider and clears the worker flag.
                loader._shutdown_enabled_var.set(False)
                loader._shutdown_on_change()
                self.assertFalse(loader.shutdown_worker.enabled)
                self.assertIn(["disabled"], loader._shutdown_slider.states)
                self.assertIn("未启用", loader._shutdown_status.text)

    def test_countdown_panel_applies_interval_and_dragged_remaining(self) -> None:
        from unittest import mock

        class Var:
            def __init__(self, value): self.value = value
            def get(self): return self.value
            def set(self, value): self.value = value

        class Widget:
            def __init__(self):
                self.text = ""
                self.states = []
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)
                if "text" in kwargs:
                    self.text = kwargs["text"]

            def state(self, states):
                self.states.append(states)

        class FakeCountdownWorker:
            def __init__(self):
                self.enabled = False
                self.interval = 3600.0
                self.remaining = 3600.0

            def set_interval_hours(self, hours):
                self.interval = float(hours) * 3600.0
                self.remaining = self.interval

            def set_enabled(self, enabled):
                self.enabled = bool(enabled)

            def set_remaining_seconds(self, seconds):
                self.remaining = max(0.0, min(float(seconds), self.interval))

            def snapshot(self):
                return self.enabled, self.interval, self.remaining

        worker = UiWorker.__new__(UiWorker)
        worker.countdown_worker = FakeCountdownWorker()
        worker._shutdown_enabled_var = Var(False)
        worker._shutdown_hours_var = Var(3.0)
        worker._countdown_enabled_var = Var(True)
        worker._countdown_interval_var = Var(1.0)
        worker._countdown_remaining_var = Var(3600.0)
        worker._countdown_interval_label = Widget()
        worker._countdown_interval_slider = Widget()
        worker._countdown_remaining_label = Widget()
        worker._countdown_remaining_slider = Widget()
        worker._countdown_status = Widget()
        worker._countdown_dragging = False

        with mock.patch.object(worker, "_shutdown_save_settings"):
            worker._countdown_on_change()
        self.assertTrue(worker.countdown_worker.enabled)
        self.assertEqual(worker.countdown_worker.interval, 3600.0)
        self.assertEqual(worker._countdown_interval_label.text, "1.0h")
        self.assertEqual(worker._countdown_remaining_slider.options["to"],
                         3600.0)

        worker._countdown_drag_start()
        worker._countdown_remaining_var.set(1200.0)
        worker._countdown_remaining_on_drag("1200")
        self.assertEqual(worker.countdown_worker.remaining, 1200.0)
        self.assertEqual(worker._countdown_remaining_label.text, "20m 00s")
        worker._countdown_drag_end()
        worker._refresh_countdown_status()
        self.assertIn("剩余 20m 00s", worker._countdown_status.text)

        worker._countdown_enabled_var.set(False)
        with mock.patch.object(worker, "_shutdown_save_settings"):
            worker._countdown_on_change()
        self.assertFalse(worker.countdown_worker.enabled)
        self.assertIn(["disabled"],
                      worker._countdown_remaining_slider.states)

    def test_drug_buff_rows_roundtrip_and_apply_to_status_worker(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from status_worker import StatusConfig

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
                self.text = ""

            def configure(self, *, text: str = None, style: str = None) -> None:
                if text is not None:
                    self.text = text

        class FakeStatusWorker:
            def __init__(self) -> None:
                self.detector = type("Detector", (), {"config": StatusConfig()})()

        def make_worker() -> UiWorker:
            w = UiWorker.__new__(UiWorker)
            w.status_worker = FakeStatusWorker()
            w._hp_threshold_label = Label()
            w._mp_threshold_label = Label()
            w._hp_key_button = Button()
            w._mp_key_button = Button()
            w._buff1_interval_label = Label()
            w._buff2_interval_label = Label()
            w._buff1_key_button = Button()
            w._buff2_key_button = Button()
            w._hp_key_var = Var("delete")
            w._mp_key_var = Var("end")
            w._hp_threshold_var = Var(50)
            w._mp_threshold_var = Var(30)
            w._hp_use_var = Var(True)
            w._mp_use_var = Var(True)
            w._buff1_key_var = Var("home")
            w._buff2_key_var = Var("insert")
            w._buff1_interval_var = Var(10.0)
            w._buff2_interval_var = Var(10.0)
            w._buff1_use_var = Var(False)
            w._buff2_use_var = Var(False)
            w._drug_status = Label()
            return w

        with tempfile.TemporaryDirectory() as directory:
            worker = make_worker()
            worker._buff1_use_var.set(True)
            worker._buff1_interval_var.set(12.5)
            worker._buff1_key_var.set("pagedown")
            worker._buff2_use_var.set(True)

            def fake_path(self):
                return Path(directory) / "drug_settings.json"

            with mock.patch.object(UiWorker, "_drug_settings_path", fake_path):
                worker._drug_on_change()
                saved = Path(directory) / "drug_settings.json"
                self.assertTrue(saved.is_file())
                data = json.loads(saved.read_text(encoding="utf-8"))
                self.assertTrue(data["buff1_enabled"])
                self.assertEqual(data["buff1_interval"], 12.5)
                self.assertEqual(data["buff1_key"], "pagedown")
                self.assertTrue(data["buff2_enabled"])
                # Applied LIVE to the status worker detector config.
                config = worker.status_worker.detector.config
                self.assertTrue(config.buff1_enabled)
                self.assertAlmostEqual(config.buff1_interval, 750.0)
                self.assertEqual(config.buff1_key, "pagedown")
                self.assertIn("增益1", worker._drug_status.text)
                self.assertIn("12.5分钟", worker._drug_status.text)

                loader = make_worker()
                with mock.patch.object(UiWorker, "_drug_settings_path",
                                       fake_path):
                    loader._drug_load_settings()
                self.assertTrue(loader._buff1_use_var.get())
                self.assertEqual(loader._buff1_interval_var.get(), 12.5)
                self.assertEqual(loader._buff1_key_var.get(), "pagedown")
                self.assertTrue(loader._buff2_use_var.get())
                self.assertTrue(
                    loader.status_worker.detector.config.buff1_enabled
                )
                self.assertEqual(
                    loader._buff1_interval_label.text, "12.5min"
                )

    def test_player_check_selection_persists_and_applies_to_mover(self) -> None:
        import json
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
            def configure(self, *, text: str) -> None:
                pass

        class Slider:
            def state(self, args) -> None:
                pass

        class FakeShutdownWorker:
            def __init__(self) -> None:
                self.enabled = False
                self.hours = 3.0

            def set_hours(self, hours) -> None:
                self.hours = hours

        class FakeMover:
            def __init__(self) -> None:
                self.calls = []

            def set_other_player_check(self, enabled) -> None:
                self.calls.append(enabled)

        class FakeCharacter:
            def __init__(self) -> None:
                self.calls = []
                self.sound_calls = []

            def set_disconnect_alert(self, enabled) -> None:
                self.calls.append(enabled)

            def set_sound_enabled(self, enabled) -> None:
                self.sound_calls.append(enabled)

        class FakeLieDetector:
            def __init__(self) -> None:
                self.calls = []
                self.sound_calls = []

            def set_enabled(self, enabled) -> None:
                self.calls.append(enabled)

            def set_sound_enabled(self, enabled) -> None:
                self.sound_calls.append(enabled)

        worker = UiWorker.__new__(UiWorker)
        worker.shutdown_worker = FakeShutdownWorker()
        worker.movement_worker = FakeMover()
        worker.character_worker = FakeCharacter()
        worker.lie_detector_worker = FakeLieDetector()
        worker._shutdown_enabled_var = Var(False)
        worker._shutdown_hours_var = Var(3.0)
        worker._shutdown_hours_label = Label()
        worker._shutdown_slider = Slider()
        worker._shutdown_status = Label()
        worker._player_check_var = Var(True)
        worker._disconnect_alert_var = Var(True)
        worker._lie_alert_var = Var(True)
        worker._sound_alert_var = Var(False)

        def fake_path(self):
            return Path(tempfile.gettempdir()) / "test_player_check_settings.json"

        with mock.patch.object(UiWorker, "_shutdown_settings_path", fake_path):
            worker._shutdown_on_change()
            data = json.loads(
                (Path(tempfile.gettempdir())
                 / "test_player_check_settings.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(data["player_check_enabled"])
            self.assertTrue(data["disconnect_alert_enabled"])
            self.assertTrue(data["lie_alert_enabled"])
            self.assertFalse(data["sound_alert_enabled"])
            self.assertEqual(worker.movement_worker.calls, [True])
            self.assertEqual(worker.character_worker.calls, [True])
            self.assertEqual(worker.lie_detector_worker.calls, [True])
            self.assertEqual(worker.character_worker.sound_calls, [False])
            self.assertEqual(worker.lie_detector_worker.sound_calls, [False])

            loader = UiWorker.__new__(UiWorker)
            loader.shutdown_worker = FakeShutdownWorker()
            loader.movement_worker = FakeMover()
            loader.character_worker = FakeCharacter()
            loader.lie_detector_worker = FakeLieDetector()
            loader._shutdown_enabled_var = Var(False)
            loader._shutdown_hours_var = Var(3.0)
            loader._shutdown_hours_label = Label()
            loader._shutdown_slider = Slider()
            loader._shutdown_status = Label()
            loader._player_check_var = Var(False)
            loader._disconnect_alert_var = Var(False)
            loader._lie_alert_var = Var(False)
            loader._sound_alert_var = Var(True)
            with mock.patch.object(UiWorker, "_shutdown_settings_path", fake_path):
                loader._shutdown_load_settings()
            self.assertTrue(loader._player_check_var.get())
            self.assertEqual(loader.movement_worker.calls, [True])
            self.assertTrue(loader._disconnect_alert_var.get())
            self.assertEqual(loader.character_worker.calls, [True])
            self.assertTrue(loader._lie_alert_var.get())
            self.assertEqual(loader.lie_detector_worker.calls, [True])
            self.assertFalse(loader._sound_alert_var.get())
            self.assertEqual(loader.character_worker.sound_calls, [False])
            self.assertEqual(loader.lie_detector_worker.sound_calls, [False])


class WindowGeometryHelperTests(unittest.TestCase):
    def test_parse_window_geometry_accepts_position_and_negative_x(self) -> None:
        self.assertEqual(
            _parse_window_geometry("1200x1000+40+40"),
            (1200, 1000, 40, 40),
        )
        self.assertEqual(
            _parse_window_geometry("980x560+120-40"),
            (980, 560, 120, -40),
        )

    def test_parse_window_geometry_rejects_malformed_input(self) -> None:
        self.assertIsNone(_parse_window_geometry(""))
        self.assertIsNone(_parse_window_geometry("1200x1000"))
        self.assertIsNone(_parse_window_geometry("1200x1000+40"))
        self.assertIsNone(_parse_window_geometry("0x0+0+0"))
        self.assertIsNone(_parse_window_geometry("1200x0+40+40"))
        self.assertIsNone(_parse_window_geometry("not-a-geometry"))

    def test_clamp_window_geometry_keeps_window_fully_on_screen(self) -> None:
        # A previously saved position can be off-screen after a monitor
        # change; the restored window must always be reachable/draggable.
        clamped = _clamp_window_geometry("1200x1000+1500+900", 1920, 1080)
        width, height, x, y = _parse_window_geometry(clamped)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 1920)
        self.assertLessEqual(y + height, 1080)
        # The size itself is preserved when it fits.
        self.assertEqual((width, height), (1200, 1000))

    def test_clamp_window_geometry_enforces_minimum_size(self) -> None:
        width, height, _, _ = _parse_window_geometry(
            _clamp_window_geometry("400x300+0+0", 1920, 1080)
        )
        self.assertGreaterEqual(width, 980)
        self.assertGreaterEqual(height, 560)

    def test_clamp_window_geometry_caps_initial_height(self) -> None:
        width, height, _, _ = _parse_window_geometry(
            _clamp_window_geometry(
                "1200x1800+40+40", 2560, 1440, max_height=1250
            )
        )
        self.assertEqual(width, 1200)
        self.assertEqual(height, 1250)

    def test_clamp_window_geometry_falls_back_for_corrupt_input(self) -> None:
        self.assertEqual(
            _clamp_window_geometry("garbage", 1920, 1080),
            "980x560+40+40",
        )

    def test_window_geometry_settings_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            fake_path = Path(directory) / "ui_window_settings.json"
            with mock.patch(
                "ui_worker._window_geometry_settings_path",
                return_value=fake_path,
            ):
                _save_window_geometry("1100x800+60+70")
                self.assertEqual(
                    _load_window_geometry("1200x1000+40+40"),
                    "1100x800+60+70",
                )

    def test_load_window_geometry_falls_back_to_default(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with mock.patch(
                "ui_worker._window_geometry_settings_path",
                return_value=missing,
            ):
                self.assertEqual(
                    _load_window_geometry("1200x1000+40+40"),
                    "1200x1000+40+40",
                )
            corrupt = Path(directory) / "corrupt.json"
            corrupt.write_text("not json", encoding="utf-8")
            with mock.patch(
                "ui_worker._window_geometry_settings_path",
                return_value=corrupt,
            ):
                self.assertEqual(
                    _load_window_geometry("1200x1000+40+40"),
                    "1200x1000+40+40",
                )


if __name__ == "__main__":
    unittest.main()
