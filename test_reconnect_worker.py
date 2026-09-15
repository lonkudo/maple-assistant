# -*- coding: utf-8 -*-
"""自动重连: the login-page colour gate, the click geometry and the reconnect sequence.

The flow is fixed by the operator: 掉线 event -> the game window must show the login page's
base colour (``screenshots/login_page_target.jpg`` is the colour reference) -> Enter -> 3s ->
click the chosen world row -> click the first channel -> move to the target channel with
right/down -> Enter -> 2s -> Enter.  Ticking the 自动重连 box only ARMS the worker; the drill
starts on a 掉线 event or on the temporary 测试重连 button.

These tests cover the parts that can be checked without a game window: the colour gate, the
geometry (which is what makes a click land on the right row / channel), the 1-60 input rule,
and the order of the actions with fake screen, clicker and keyboard.
"""

import queue
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from reconnect_worker import (
    CALIBRATION_STEPS,
    CHANNEL_MAX,
    CHANNEL_MIN,
    ReconnectLayout,
    ReconnectWorker,
    WORLD_NAMES,
    find_login_page,
    load_layout,
    load_login_reference,
    valid_channel,
)

LAYOUT = ReconnectLayout(
    world_first_row_centre=(500, 200),
    world_row_pitch=40,
    first_channel_centre=(300, 300),
    channel_pitch=(50, 45),
    channels_per_row=5,
)


class ChannelInputTests(unittest.TestCase):
    def test_only_integers_1_to_60_are_accepted(self):
        self.assertEqual(CHANNEL_MIN, 1)
        self.assertEqual(CHANNEL_MAX, 60)
        for good in (1, 60, "1", "60", " 7 ", 7):
            self.assertEqual(valid_channel(good), int(str(good).strip()), good)
        for bad in (0, 61, -1, "0", "61", "abc", "", None, "3.5", 3.5, "1e2"):
            self.assertIsNone(valid_channel(bad), bad)


class ClipGeometryTests(unittest.TestCase):
    """Where a click lands - measured rows and the channel grid."""

    def test_world_rows_follow_the_measured_pitch(self):
        self.assertEqual(LAYOUT.world_click_point(0, (1366, 768)), (500, 200))
        self.assertEqual(LAYOUT.world_click_point(1, (1366, 768)), (500, 240))
        self.assertEqual(LAYOUT.world_click_point(4, (1366, 768)), (500, 360))
        # out of range indices clamp to the list
        self.assertEqual(LAYOUT.world_click_point(9, (1366, 768)), (500, 360))
        self.assertEqual(len(WORLD_NAMES), 5)

    def test_channel_cells_follow_the_grid(self):
        self.assertEqual(LAYOUT.channel_click_point(1, (1366, 768)), (300, 300))
        self.assertEqual(LAYOUT.channel_click_point(3, (1366, 768)), (400, 300))
        self.assertEqual(LAYOUT.channel_click_point(6, (1366, 768)), (300, 345))
        self.assertEqual(LAYOUT.channel_click_point(11, (1366, 768)), (300, 390))

    def test_keyboard_moves_move_right_then_down_from_channel_one(self):
        self.assertEqual(LAYOUT.channel_key_moves(1), [])
        self.assertEqual(LAYOUT.channel_key_moves(2), ["right"])
        self.assertEqual(LAYOUT.channel_key_moves(5), ["right"] * 4)
        self.assertEqual(LAYOUT.channel_key_moves(6), ["down"])
        self.assertEqual(LAYOUT.channel_key_moves(8), ["right", "right", "down"])
        self.assertEqual(LAYOUT.channel_key_moves(11), ["down", "down"])
        self.assertEqual(
            LAYOUT.channel_key_moves(60), ["right"] * 4 + ["down"] * 11
        )
        # every move ends on the channel the operator asked for
        for channel in (1, 2, 5, 6, 7, 10, 25, 59, 60):
            moves = LAYOUT.channel_key_moves(channel)
            row, column = 0, 0
            for key in moves:
                if key == "right":
                    column += 1
                elif key == "down":
                    row += 1
            self.assertEqual(row * 5 + column + 1, channel, (channel, moves))

    def test_the_layout_scales_with_the_live_client(self):
        # 1920x1080 uses the same game-UI preset, so everything scales by the size ratio.
        scaled = LAYOUT.scaled((1920, 1080))
        self.assertEqual(scaled.world_first_row_centre, (round(500 * 1920 / 1366),
                                                        round(200 * 1080 / 768)))
        self.assertEqual(scaled.first_channel_centre, (round(300 * 1920 / 1366),
                                                       round(300 * 1080 / 768)))
        self.assertEqual(scaled.channel_pitch, (round(50 * 1920 / 1366),
                                                round(45 * 1080 / 768)))
        # a non-uniform client keeps the same proportion per axis
        wide = LAYOUT.scaled((1366, 768 * 2))
        self.assertEqual(wide.channel_pitch[1], 90)


class LoginPageColourTests(unittest.TestCase):
    """Sign two: the window must show the login page's base colour, over one big region.

    The reference crop is a patch of the page's cream background, so it defines a COLOUR to
    look for (base + narrow range), not a shape to correlate.  The negatives are the
    operator's own frames: two select windows (cream themed, 11.7 % one region) and three
    in-game frames (0.0-0.1 %).
    """

    @staticmethod
    def _reference():
        from reconnect_worker import login_colour_reference

        patch = np.zeros((200, 200, 3), dtype=np.uint8)
        patch[:, :] = (216, 232, 248)                      # cream, BGR
        patch[:100, :100] = (208, 224, 240)                # its own texture
        return login_colour_reference(patch)

    def test_the_base_colour_and_range_come_from_the_reference(self):
        reference = self._reference()
        self.assertIsNotNone(reference)
        for channel, expected in enumerate((216, 232, 248)):
            self.assertAlmostEqual(reference.base_bgr[channel], expected, delta=2)
            self.assertLessEqual(reference.lower_bgr[channel], expected)
            self.assertGreaterEqual(reference.upper_bgr[channel], expected)
        self.assertEqual(reference.pixels, 200 * 200)

    def test_a_login_page_frame_is_accepted(self):
        from reconnect_worker import find_login_page

        frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        frame[100:700, 300:1100] = (216, 232, 248)         # 46 % of the window, one region
        evidence = find_login_page(frame, self._reference())
        self.assertIsNotNone(evidence)
        self.assertGreater(evidence.fraction, 0.4)
        self.assertEqual(evidence.area, 600 * 800)
        self.assertEqual(evidence.bbox, (300, 100, 800, 600))

    def test_a_frame_that_is_not_the_login_page_is_rejected(self):
        from reconnect_worker import find_login_page

        in_game = np.zeros((768, 1366, 3), dtype=np.uint8)
        in_game[:, :] = (40, 120, 60)                      # grass
        self.assertIsNone(find_login_page(in_game, self._reference()))
        # a small patch of the page colour is not enough either (window chrome, a button)
        small = np.zeros((768, 1366, 3), dtype=np.uint8)
        small[200:260, 400:520] = (216, 232, 248)
        self.assertIsNone(find_login_page(small, self._reference()))

    def test_the_operators_own_frames_separate_cleanly(self):
        import cv2

        from reconnect_worker import (LOGIN_PAGE_MIN_FRACTION, analyse_login_page,
                                      find_login_page, load_login_reference)

        folder = Path(__file__).resolve().parent / "screenshots"
        reference = load_login_reference(folder / "login_page_target.jpg")
        self.assertIsNotNone(reference, "the operator's own reference must load")
        # the two select windows are cream themed: one region covers 11.7 % of the window
        for name in ("channel_select_first.jpg", "channel_select_second.jpg"):
            frame = cv2.imread(str(folder / name), cv2.IMREAD_COLOR)
            if frame is None:
                self.skipTest(f"{name} is missing")
            evidence = analyse_login_page(frame, reference)
            self.assertGreaterEqual(evidence.fraction, LOGIN_PAGE_MIN_FRACTION, name)
            self.assertIsNotNone(find_login_page(frame, reference), name)
        # ... and the in-game frames are nowhere near the threshold
        for name in ("second_window.jpg", "frame_000010.jpg"):
            frame = cv2.imread(str(folder / name), cv2.IMREAD_COLOR)
            if frame is None:
                self.skipTest(f"{name} is missing")
            evidence = analyse_login_page(frame, reference)
            self.assertLess(evidence.fraction, LOGIN_PAGE_MIN_FRACTION, name)
            self.assertIsNone(find_login_page(frame, reference), name)

    def test_a_missing_reference_file_is_reported_not_raised(self):
        from reconnect_worker import load_login_reference

        self.assertIsNone(load_login_reference(Path("does/not/exist.jpg")))
        self.assertIsNotNone(load_login_reference(
            Path(__file__).resolve().parent / "screenshots" / "login_page_target.jpg"))

    def test_the_reference_is_found_without_a_path(self):
        """The packaged copy lives in recording-assets, the personal one in screenshots."""

        from reconnect_worker import LOGIN_REFERENCE_PATHS, load_login_reference

        names = [Path(entry).name for entry in LOGIN_REFERENCE_PATHS]
        self.assertEqual(names.count("login_page_target.jpg"), len(names))
        folders = {Path(entry).parent.name for entry in LOGIN_REFERENCE_PATHS}
        self.assertEqual(folders, {"screenshots", "recording-assets"})
        # at least one of them exists in this checkout, and the loader finds it
        reference = load_login_reference()
        self.assertIsNotNone(reference)


class _FakeSender:
    def __init__(self):
        self.keys = []
        self.selected = 0

    def select_window(self):
        self.selected += 1
        return True

    def is_game_foreground(self):
        return True

    def press(self, key, duration=0.025):
        self.keys.append(key)
        return True


class _FakeClicker:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))
        return True


class ReconnectSequenceTests(unittest.TestCase):
    """The order of actions, with a fake screen, clicker, keyboard and clock.

    The template is generated here (with real structure) because the operator's bundled
    `login_page_target.jpg` is a flat patch that the worker rightly refuses to trust.
    """

    def setUp(self):
        import cv2
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        # the colour reference: a crop of the login page's cream background
        reference = np.zeros((200, 200, 3), dtype=np.uint8)
        reference[:, :] = (216, 232, 248)
        reference[:80, :80] = (206, 222, 238)
        self.template_path = folder / "login_target.jpg"
        cv2.imwrite(str(self.template_path), reference)
        # a game window showing the login page: one large region of that colour
        login_frame = np.zeros((768, 1366, 3), dtype=np.uint8)
        login_frame[:, :] = (30, 40, 60)
        login_frame[80:700, 260:1120] = (216, 232, 248)
        self.login_frame = login_frame

    def tearDown(self):
        self._tmp.cleanup()

    def _worker(self, *, frame, rect=(100, 50, 1466, 818), enabled=True, sender=None,
                clicker=None, layout=LAYOUT):
        self.sleeps = []

        def capture():
            return frame, rect

        worker = ReconnectWorker(
            sender if sender is not None else _FakeSender(),
            threading.Event(),
            queue.Queue(maxsize=32),
            layout=layout,
            capture_fn=capture,
            clicker=clicker if clicker is not None else _FakeClicker(),
            template_path=self.template_path,
            sleep=lambda seconds: self.sleeps.append(seconds),
        )
        worker.set_enabled(enabled)
        return worker

    def test_the_sequence_is_enter_world_first_channel_moves_enter_enter(self):
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=self.login_frame, sender=sender, clicker=clicker)
        worker.set_world("绿水灵")
        worker.set_channel(8)
        worker._handle_disconnect()

        # window brought forward, Enter sent, then the two clicks, the channel moves and
        # the second Enter that starts the login (the operator's "Enter -> 2s -> Enter")
        self.assertEqual(sender.selected, 1)
        self.assertEqual(sender.keys,
                         ["enter", "right", "right", "down", "enter", "enter"])
        self.assertEqual(
            clicker.clicks,
            [(100 + 500, 50 + 200 + 2 * 40),        # 绿水灵 is row 3
             (100 + 300, 50 + 300)],               # the first channel
        )
        # the operator's pauses: 3s after Enter, the select-window pauses, then 2s between
        # the two Enters at the end
        self.assertIn(3.0, self.sleeps)
        self.assertIn(2.0, self.sleeps)
        self.assertLess(self.sleeps.index(3.0), self.sleeps.index(2.0),
                        "the 3s pause belongs to the login page, the 2s one to the channel")

    def test_a_manual_test_runs_even_when_the_checkbox_is_off(self):
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=self.login_frame, sender=sender,
                              clicker=clicker, enabled=False)
        worker.set_world("蘑菇仔")
        worker.set_channel(3)
        # the 掉线 trigger stays disabled ...
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        # ... but the temporary test button runs the whole sequence once
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        self.assertEqual(sender.keys,
                         ["enter", "right", "right", "enter", "enter"])
        self.assertEqual(len(clicker.clicks), 2)
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertIn(("done", "蘑菇仔 3频道"), reports)

    def test_a_manual_test_does_not_need_the_login_page_colour(self):
        """The button is pressed while the operator looks at the login page themselves."""

        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8),
                              sender=sender, clicker=clicker, enabled=False)
        worker.set_channel(1)
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        # no login page colour in the frame, and the sequence still ran end to end
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertEqual(len(clicker.clicks), 2)

    def test_a_manual_test_reports_the_login_page_colour_measurement(self):
        worker = self._worker(frame=self.login_frame)
        evidence = worker.measure_login_page_colour()
        self.assertIsNotNone(evidence)
        self.assertAlmostEqual(evidence.fraction, 620 * 860 / (768 * 1366), delta=0.01)
        self.assertTrue(worker.trigger_test())
        worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        colours = [detail for state, detail in reports if state == "colour"]
        self.assertTrue(colours, reports)
        self.assertIn("判定为登录页", colours[-1])
        self.assertIn("阈值", colours[-1])

    def test_the_test_button_is_refused_while_a_run_is_going_on(self):
        worker = self._worker(frame=self.login_frame)
        self.assertTrue(worker.trigger_test())
        self.assertFalse(worker.trigger_test())
        self.assertFalse(worker.trigger_test())
        worker._handle_disconnect()               # the queued run is consumed
        self.assertTrue(worker.trigger_test(), "a finished run must re-arm the button")

    def test_without_the_login_page_colour_nothing_is_pressed(self):
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8),
                              sender=sender, clicker=clicker)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        self.assertEqual(clicker.clicks, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(failed, reports)
        self.assertIn("登录页", " ".join(failed), reports)

    @staticmethod
    def _loop_once(worker):
        """One iteration of ReconnectWorker.run()'s loop (the real trigger path)."""

        if worker._wake.wait(0.05):
            worker._wake.clear()
            worker._handle_disconnect()

    def test_enabling_the_checkbox_only_arms_the_worker(self):
        """The operator's rule: ticking 自动重连 must not run the drill."""

        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=self.login_frame, sender=sender,
                              clicker=clicker, enabled=False)
        worker.set_enabled(True)
        # arming must not wake the loop, so the loop finds nothing to do
        self.assertFalse(worker._wake.wait(0.05),
                         "enabling must not wake the worker for a run")
        self._loop_once(worker)
        self.assertEqual(sender.keys, [])
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(sender.selected, 0)
        # only the 掉线 event (or the test button) starts the sequence
        worker.notify_disconnect()
        self._loop_once(worker)
        self.assertEqual(sender.keys[0], "enter")
        self.assertEqual(len(clicker.clicks), 2)

    def test_disabled_means_no_action_at_all(self):
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=self.login_frame, sender=sender,
                              clicker=clicker, enabled=False)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        self.assertEqual(clicker.clicks, [])
        self.assertEqual(sender.selected, 0)

    def test_the_channel_of_channel_one_needs_no_keyboard_move(self):
        sender = _FakeSender()
        worker = self._worker(frame=self.login_frame, sender=sender)
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])

    def test_the_settings_are_validated(self):
        worker = self._worker(frame=self.login_frame)
        self.assertTrue(worker.set_world("蘑菇仔"))
        self.assertFalse(worker.set_world("没有这个区"))
        self.assertTrue(worker.set_channel(7))
        self.assertFalse(worker.set_channel(0))
        self.assertFalse(worker.set_channel(61))
        self.assertFalse(worker.set_channel("abc"))
        self.assertFalse(worker.set_channel(3.5))
        _, world, channel = worker.settings()
        self.assertEqual((world, channel), ("蘑菇仔", 7))


class PanelInputTests(unittest.TestCase):
    """The 自动重连 box in the Additional Functions panel only takes 1-60."""

    def test_the_spinbox_validator_rejects_everything_but_1_to_60(self):
        from ui_worker import UiWorker

        validator = UiWorker._validate_reconnect_channel
        for good in ("1", "9", "60", ""):        # "" = mid-edit, checked on change
            self.assertTrue(validator(None, good), good)
        for bad in ("0", "61", "61 ", "12a", "-1", "1.5", "abc"):
            self.assertFalse(validator(None, bad), bad)

    def test_the_change_handler_writes_all_three_settings(self):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Worker:
            def __init__(self):
                self.calls = []

            def set_world(self, world):
                self.calls.append(("world", world))
                return True

            def set_channel(self, channel):
                self.calls.append(("channel", channel))
                return True

            def set_enabled(self, enabled):
                self.calls.append(("enabled", enabled))

        class _Status:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", "")

        class _Ui:
            _validate_reconnect_channel = UiWorker._validate_reconnect_channel
            _reconnect_on_change = UiWorker._reconnect_on_change
            _reconnect_test_idle = UiWorker._reconnect_test_idle
            _save_reconnect_settings = UiWorker._save_reconnect_settings

            def __init__(self):
                self._reconnect_var = _Var(True)
                self._reconnect_world_var = _Var("漂漂猪")
                self._reconnect_channel_var = _Var("12")
                self._reconnect_status = _Status()
                self.reconnect_worker = _Worker()
                self.saved = None

            def _shutdown_save_settings(self, data):
                self.saved = data

            def _shutdown_collect_data(self):
                return {
                    "auto_reconnect_enabled": bool(self._reconnect_var.get()),
                    "auto_reconnect_world": self._reconnect_world_var.get(),
                    "auto_reconnect_channel": int(self._reconnect_channel_var.get()),
                }

        ui = _Ui()
        ui._reconnect_on_change()
        self.assertEqual(ui.reconnect_worker.calls,
                         [("world", "漂漂猪"), ("channel", 12), ("enabled", True)])
        self.assertEqual(ui.saved["auto_reconnect_world"], "漂漂猪")
        self.assertEqual(ui.saved["auto_reconnect_channel"], 12)
        self.assertIn("漂漂猪", ui._reconnect_status.text)

    def test_an_out_of_range_channel_falls_back_to_the_default(self):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Worker:
            def __init__(self):
                self.calls = []

            def set_world(self, world):
                return True

            def set_channel(self, channel):
                self.calls.append(channel)
                return True

            def set_enabled(self, enabled):
                pass

        class _Ui:
            _validate_reconnect_channel = UiWorker._validate_reconnect_channel
            _reconnect_on_change = UiWorker._reconnect_on_change
            _reconnect_test_idle = UiWorker._reconnect_test_idle

            def __init__(self):
                self._reconnect_var = _Var(False)
                self._reconnect_world_var = _Var("蓝蜗牛")
                self._reconnect_channel_var = _Var("99")   # typed past the limit
                self._reconnect_status = type("S", (), {
                    "configure": lambda self, **kw: None})()
                self.reconnect_worker = _Worker()

            def _save_reconnect_settings(self, *args):
                pass

        ui = _Ui()
        ui._reconnect_on_change()
        self.assertEqual(ui._reconnect_channel_var.get(), "1",
                         "an out-of-range entry must fall back to a valid channel")
        self.assertEqual(ui.reconnect_worker.calls, [1])


class TemporaryTestButtonTests(unittest.TestCase):
    """The temporary 测试重连 button: trigger the sequence now, re-arm when it is over."""

    @staticmethod
    def _ui(worker):
        from ui_worker import UiWorker

        class _Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class _Status:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", "")

        class _Button:
            def __init__(self):
                self.state = "normal"

            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)

        class _Uri(UiWorker):
            def __init__(self):
                self._reconnect_var = _Var(False)      # checkbox deliberately off
                self._reconnect_world_var = _Var("小白兔")
                self._reconnect_channel_var = _Var("4")
                self._reconnect_status = _Status()
                self._reconnect_test_button = _Button()
                self._reconnect_capture_button = _Button()
                self.reconnect_worker = worker
                self.error_log_blocks = []

            def _save_reconnect_settings(self, *args):
                pass

            def _play_action_sound(self, success):      # no mp3 during tests
                pass

            def _append_error_log(self, text, tag):     # never touch the real error.log
                self.error_log_blocks.append((tag, text))

        return _Uri()

    def test_the_button_applies_the_settings_and_starts_one_run(self):
        started = []

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                started.append(True)
                return True

        ui = self._ui(_Worker())
        ui._reconnect_test_clicked()
        self.assertEqual(
            (ui._reconnect_world_var.get(), ui._reconnect_channel_var.get()),
            ("小白兔", "4"),
        )
        self.assertEqual(len(started), 1)
        self.assertEqual(ui._reconnect_test_button.state, "disabled")
        self.assertIn("手动测试", ui._reconnect_status.text)

    def test_a_run_in_progress_is_reported_and_a_finished_run_re_arms(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return False                            # already running

        ui = self._ui(_Worker())
        ui._reconnect_test_clicked()
        self.assertIn("已有一次重连", ui._reconnect_status.text)

        # the worker's own report re-arms the button
        results = _queue.Queue()
        results.put(("done", "小白兔 4频道"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertEqual(ui._reconnect_test_button.state, "normal")

    def test_the_capture_button_saves_a_frame_and_shows_the_file(self):
        import tempfile

        saved = []

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def save_diagnostic_capture(self, prefix="select_window", folder=None):
                saved.append(True)
                return Path("C:/somewhere/select_window_20260914_132000.png")

        ui = self._ui(_Worker())
        ui._reconnect_capture_clicked()
        self.assertEqual(len(saved), 1)
        self.assertIn("select_window_20260914_132000.png", ui._reconnect_status.text)
        self.assertEqual(ui._reconnect_capture_button.state, "normal")

    def test_a_failure_is_written_to_error_log_with_the_settings(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return True

        ui = self._ui(_Worker())
        results = _queue.Queue()
        results.put(("failed", "实时输入未开启（请点击 开始巡逻 启用输入后再试）"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertIn("自动重连失败", ui._reconnect_status.text)
        self.assertEqual(len(ui.error_log_blocks), 1)
        tag, text = ui.error_log_blocks[0]
        self.assertEqual(tag, "auto reconnect")
        self.assertIn("实时输入未开启", text)
        self.assertIn("小白兔", text)                   # the run's own settings
        self.assertIn("频道=4", text)

    def test_the_login_page_colour_measurement_is_shown(self):
        import queue as _queue

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def trigger_test(self):
                return True

        ui = self._ui(_Worker())
        results = _queue.Queue()
        results.put(("colour", "同色区域 50.8%（阈值 6.0%），判定为登录页"))
        ui.reconnect_results = results
        ui._drain_reconnect_results()
        self.assertIn("登录页颜色检测", ui._reconnect_status.text)
        self.assertIn("50.8", ui._reconnect_status.text)


class _ArmableFakeSender(_FakeSender):
    """A sender whose live input must be armed before it delivers any key.

    This is what WindowKeySender does in the field (`input_is_enabled` is False until
    开始巡逻/Start Patrol), and the reason the first field test ended with
    "key enter was not claimed".
    """

    def __init__(self, *, enabled: bool = False):
        super().__init__()
        self.input_enabled = bool(enabled)
        self.armed = 0
        self.disarmed = 0

    def input_is_enabled(self):
        return self.input_enabled

    def enable_input(self):
        self.armed += 1
        self.input_enabled = True

    def disable_input(self, **kwargs):
        self.disarmed += 1
        self.input_enabled = False

    def press(self, key, duration=0.025):
        if not self.input_enabled:
            return False                       # exactly the field behaviour
        self.keys.append(key)
        return True


class LiveInputTests(unittest.TestCase):
    """The reconnect must be able to send keys, and every failure must reach error.log."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def _worker(self, **kwargs):
        return self._case._worker(**kwargs)

    def test_the_sequence_arms_live_input_and_puts_the_state_back(self):
        sender = _ArmableFakeSender(enabled=False)
        worker = self._worker(frame=self._case.login_frame, sender=sender)
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertEqual(sender.armed, 1, "a disarmed sender must be armed for the drill")
        self.assertEqual(sender.disarmed, 1, "and disarmed again afterwards")
        self.assertFalse(sender.input_enabled)
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertTrue(any(state == "input" for state, _detail in reports), reports)
        self.assertIn(("done", "蓝蜗牛 1频道"), reports)

    def test_an_already_armed_input_is_left_alone(self):
        sender = _ArmableFakeSender(enabled=True)
        worker = self._worker(frame=self._case.login_frame, sender=sender)
        worker.set_channel(1)
        worker._handle_disconnect()
        self.assertEqual(sender.keys, ["enter", "enter", "enter"])
        self.assertEqual((sender.armed, sender.disarmed), (0, 0))
        self.assertTrue(sender.input_enabled)

    def test_a_key_that_cannot_be_sent_says_why_and_logs_an_error(self):
        class _BlockedSender(_FakeSender):
            """Disarmed input and no way to arm it (no enable_input)."""

            def input_is_enabled(self):
                return False

        sender = _BlockedSender()
        clicker = _FakeClicker()
        worker = self._worker(frame=self._case.login_frame, sender=sender, clicker=clicker)
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        self.assertEqual(clicker.clicks, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("实时输入未开启" in detail for detail in failed), reports)
        self.assertTrue(any("live input is disarmed" in line for line in captured.output),
                        captured.output)

    def test_a_window_that_cannot_be_captured_is_reported_as_such(self):
        """No frame at all is not "not the login page" - it must say so."""

        worker = self._worker(frame=None)
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("无法截取游戏窗口" in detail for detail in failed), reports)
        self.assertTrue(any("could not be captured" in line for line in captured.output),
                        captured.output)

    def test_the_real_key_sender_is_armed_so_enter_reaches_the_game(self):
        """The field failure end to end, with the real WindowKeySender.

        Measured in the field (13:01:57): live input was disarmed, so `press` refused every
        key - `key enter was not claimed` - and Enter never reached the game.  With the real
        sender class here, nothing at all may be emitted unless the worker arms it first.
        """

        from status_worker import WindowKeySender

        sender = WindowKeySender("game", dry_run=False, input_enabled=False)
        sender.select_window = lambda: True
        sender.is_game_foreground = lambda: True
        sender._foreground_matches = lambda: True
        emitted = []
        sender._send_scan_code = lambda code, key_up, extended: emitted.append(
            (code, key_up, extended))
        self.assertFalse(sender.press("enter"), "a disarmed sender refuses keys (field case)")

        worker = self._worker(frame=self._case.login_frame, sender=sender)
        worker.set_channel(1)
        worker._handle_disconnect()

        enter_code = WindowKeySender._SCAN["enter"][0]
        downs = [event for event in emitted if event[0] == enter_code and event[1] is False]
        self.assertEqual(len(downs), 3, f"three Enters must reach the game: {emitted}")
        self.assertFalse(sender.input_is_enabled(),
                         "the previous (disarmed) state must be restored")
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertIn(("done", "蓝蜗牛 1频道"), reports)

    def test_every_failure_is_logged_at_error_level(self):
        """error.log only receives ERROR+, so a stopped run must be an ERROR record."""

        worker = self._worker(frame=np.zeros((768, 1366, 3), dtype=np.uint8))
        with self.assertLogs("reconnect_worker", level="ERROR") as captured:
            worker._handle_disconnect()
        self.assertTrue(
            any("auto reconnect failed" in line for line in captured.output),
            captured.output,
        )


class UncalibratedLayoutTests(unittest.TestCase):
    """An unmeasured layout must stop the run instead of clicking the window corner.

    Measured in the field (13:20:00): "clicking world 蓝蜗牛 (row 1) at client (0, 0)" - the
    built-in layout is all zeros, so the click landed on the window's top-left corner and the
    run still reported success.
    """

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()

    def tearDown(self):
        self._case.tearDown()

    def test_the_built_in_layout_is_not_calibrated(self):
        from reconnect_worker import RECONNECT_LAYOUT, ReconnectLayout

        self.assertFalse(ReconnectLayout().is_calibrated)
        self.assertFalse(RECONNECT_LAYOUT.is_calibrated)
        self.assertTrue(LAYOUT.is_calibrated)
        # a layout with rows but no channel grid is not enough either
        partial = ReconnectLayout(world_first_row_centre=(500, 200), world_row_pitch=40)
        self.assertFalse(partial.is_calibrated)

    def test_an_uncalibrated_run_clicks_nothing_and_logs_an_error(self):
        from reconnect_worker import ReconnectLayout

        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = self._case._worker(frame=self._case.login_frame, sender=sender,
                                    clicker=clicker,
                                    layout=ReconnectLayout())        # all zeros
        worker._handle_disconnect()
        self.assertEqual(clicker.clicks, [], "no click may be sent without geometry")
        self.assertEqual(sender.keys, [], "no key may be sent without geometry")
        self.assertEqual(sender.selected, 1, "the window is still brought forward")
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("未标定" in detail for detail in failed), reports)
        self.assertFalse(any(state == "done" for state, _d in reports), reports)

    def test_a_diagnostic_capture_is_written_and_reported(self):
        import tempfile

        worker = self._case._worker(frame=self._case.login_frame)
        with tempfile.TemporaryDirectory() as folder:
            path = worker.save_diagnostic_capture(folder=Path(folder))
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).is_file())
            self.assertGreater(Path(path).stat().st_size, 0)
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertTrue(any(state == "capture" for state, _d in reports), reports)

    def test_a_capture_without_a_game_window_reports_nothing_written(self):
        import tempfile

        worker = self._case._worker(frame=None)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertLogs("reconnect_worker", level="ERROR"):
                self.assertIsNone(worker.save_diagnostic_capture(folder=Path(folder)))


class CalibrationWizardTests(unittest.TestCase):
    """The five recorded points must produce a layout the drill can click with."""

    def setUp(self):
        self._case = ReconnectSequenceTests("test_the_settings_are_validated")
        self._case.setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.layout_file = Path(self._tmp.name) / "reconnect_layout.json"

    def tearDown(self):
        self._tmp.cleanup()
        self._case.tearDown()

    def _worker(self, *, client=(1366, 768), rect=(100, 50, 1466, 818)):
        frame = self._case.login_frame
        if (frame.shape[1], frame.shape[0]) != client:
            frame = np.zeros((client[1], client[0], 3), dtype=np.uint8)
            frame[:, :] = (30, 40, 60)
            frame[80:700, 260:1120] = (216, 232, 248)      # a login page of that size
        return self._case._worker(frame=frame, rect=rect,
                                  layout=ReconnectLayout())          # uncalibrated

    def _record_all(self, worker, *, world_rows=((700, 400), (700, 430)),
                    channels=((640, 300), (690, 300), (640, 335))):
        # screen points = client point + window origin (100, 50)
        points = {
            "world_row_0": (world_rows[0][0] + 100, world_rows[0][1] + 50),
            "world_row_1": (world_rows[1][0] + 100, world_rows[1][1] + 50),
            "channel_1": (channels[0][0] + 100, channels[0][1] + 50),
            "channel_2": (channels[1][0] + 100, channels[1][1] + 50),
            "channel_6": (channels[2][0] + 100, channels[2][1] + 50),
        }
        results = []
        for key, _label in CALIBRATION_STEPS:
            results.append(worker.record_layout_point(key, points[key], self.layout_file))
        return results

    def test_the_five_points_become_a_calibrated_layout(self):
        worker = self._worker()
        self.assertFalse(worker.layout.is_calibrated)
        self.assertEqual(worker.calibration_progress(), (0, 5))
        results = self._record_all(worker)
        self.assertTrue(all(ok for ok, _message in results), results)
        self.assertTrue(worker.layout.is_calibrated)
        self.assertEqual(worker.layout.reference_client, (1366, 768))
        self.assertEqual(worker.layout.world_first_row_centre, (700, 400))
        self.assertEqual(worker.layout.world_row_pitch, 30)
        self.assertEqual(worker.layout.first_channel_centre, (640, 300))
        self.assertEqual(worker.layout.channel_pitch, (50, 35))
        self.assertEqual(worker.layout.channels_per_row, 5)
        # the last message is the summary, and the file round-trips through load_layout
        self.assertIn("标定完成", results[-1][1])
        reloaded = load_layout(self.layout_file)
        self.assertEqual(reloaded.world_first_row_centre, (700, 400))
        self.assertEqual(reloaded.channel_click_point(6, (1366, 768)), (640, 335))
        self.assertTrue(reloaded.is_calibrated)
        self.assertEqual(worker.calibration_progress(), (5, 5),
                         "a finished calibration reports five of five")

    def test_the_recorded_geometry_is_used_by_the_next_run(self):
        worker = self._worker()
        self._record_all(worker)
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker.key_sender = sender
        worker.clicker = clicker
        worker.set_world("绿水灵")               # row 3 -> two rows below the first
        worker.set_channel(6)
        worker._handle_disconnect()
        self.assertEqual(clicker.clicks,
                         [(100 + 700, 50 + 400 + 2 * 30),      # world row 3
                          (100 + 640, 50 + 300)])              # channel 1
        self.assertIn("down", sender.keys)

    def test_the_steps_are_asked_in_order_and_progress(self):
        worker = self._worker()
        order = []
        while True:
            step = worker.calibration_next_step()
            if step is None:
                break
            order.append(step[0])
            ok, _message = worker.record_layout_point(
                step[0], self._screen_point_for(step[0]), self.layout_file)
            self.assertTrue(ok, step)
        self.assertEqual(order, [key for key, _label in CALIBRATION_STEPS])
        self.assertIsNone(worker.calibration_next_step(),
                          "a finished calibration must report itself as done, not restart")
        self.assertTrue(worker.calibrated())
        self.assertEqual(worker.calibration_progress(), (5, 5))
        # and it can be redone on purpose
        worker.reset_calibration()
        self.assertFalse(worker.calibrated())
        self.assertEqual(worker.calibration_progress(), (0, 5))
        self.assertEqual(worker.calibration_next_step()[0], "world_row_0")

    @staticmethod
    def _screen_point_for(key):
        return {
            "world_row_0": (800, 450),
            "world_row_1": (800, 480),
            "channel_1": (740, 350),
            "channel_2": (790, 350),
            "channel_6": (740, 385),
        }[key]

    def test_points_that_make_no_sense_are_rejected(self):
        worker = self._worker()
        # row 2 above row 1: no pitch
        results = self._record_all(worker, world_rows=((700, 430), (700, 400)))
        self.assertTrue(all(ok for ok, _m in results[:4]))
        ok, message = results[-1]
        self.assertFalse(ok)
        self.assertIn("世界列表", message)
        self.assertFalse(worker.layout.is_calibrated,
                         "a rejected calibration must not produce a layout")
        self.assertEqual(worker.calibration_progress(), (0, 5),
                         "a rejected calibration must start over")

    def test_a_point_outside_the_game_window_is_refused(self):
        worker = self._worker()
        ok, message = worker.record_layout_point("world_row_0", (10, 10),
                                                 self.layout_file)
        self.assertFalse(ok)
        self.assertIn("不在游戏窗口内", message)
        self.assertEqual(worker.calibration_progress(), (0, 5))

    def test_the_client_size_of_the_calibration_is_kept(self):
        """A different client size scales the recorded geometry instead of breaking it."""

        from reconnect_worker import layout_from_points

        layout = layout_from_points(
            {"world_row_0": (700, 400), "world_row_1": (700, 430),
             "channel_1": (640, 300), "channel_2": (690, 300), "channel_6": (640, 335)},
            (1920, 1080),
        )
        self.assertEqual(layout.reference_client, (1920, 1080))
        scaled = layout.scaled((960, 540))
        self.assertEqual(scaled.world_first_row_centre, (350, 200))
        self.assertEqual(scaled.channel_pitch, (25, 18))

    def test_the_calibrate_button_counts_down_and_records(self):
        from ui_worker import UiWorker

        recorded = []

        class _Root:
            def after(self, delay, callback):
                recorded.append(("after", delay))
                callback()

        class _Worker:
            def set_world(self, world):
                return True

            def set_channel(self, channel):
                return True

            def set_enabled(self, enabled):
                pass

            def calibration_next_step(self):
                return ("world_row_0", "世界列表第 1 行（蓝蜗牛）")

            def record_layout_point(self, key):
                recorded.append(("record", key))
                return True, "已记录 世界列表第 1 行（700,400）"

        ui = TemporaryTestButtonTests._ui(_Worker())
        ui._root = _Root()
        ui._reconnect_calibrate_button = type("B", (), {
            "state": "normal",
            "configure": lambda self, **kw: setattr(self, "state", kw.get("state",
                                                                          self.state))})()
        ui._reconnect_calibrate_clicked()
        self.assertIn(("after", 3000), recorded)
        self.assertIn(("record", "world_row_0"), recorded)
        self.assertIn("已记录", ui._reconnect_status.text)
        self.assertEqual(ui._reconnect_calibrate_button.state, "normal")


class TkThreadingTests(unittest.TestCase):
    """The UI thread must talk to Tk through widgets, never through the UiWorker itself.

    Measured failure: `self.register(...)` for the 频道 Spinbox raised
    ``AttributeError: 'UiWorker' object has no attribute 'register'`` while the panel was
    being built, which stopped the assistant from starting at all.  `self.after(...)` was
    the same mistake waiting to happen (it only triggers when 自动重连 was saved enabled).
    """

    TK_ONLY_ON_SELF = {
        "register", "after", "after_cancel", "after_idle", "bind", "title", "geometry",
        "protocol", "mainloop", "update_idletasks", "update", "destroy", "quit",
        "winfo_children", "winfo_width", "winfo_height", "winfo_exists", "wm_attributes",
        "grab_current", "clipboard_append", "wait_variable", "wait_window",
    }

    def test_no_tk_method_is_called_on_the_worker(self):
        import ast

        source = (Path(__file__).resolve().parent / "ui_worker.py").read_text(
            encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if (name in self.TK_ONLY_ON_SELF
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                offenders.append((node.lineno, name))
        self.assertEqual(
            offenders, [],
            "Tk methods must be called on a widget or on self._root, not on the UiWorker: "
            f"{offenders}",
        )

    def test_the_channel_validator_is_registered_on_a_widget(self):
        import tkinter as tk

        from ui_worker import UiWorker

        try:
            root = tk.Tk()
        except Exception as exc:                    # no display: nothing to check here
            self.skipTest(f"Tk is not available: {exc}")
        try:
            root.withdraw()
            frame = tk.Frame(root)
            ui = UiWorker.__new__(UiWorker)          # only the helper is needed
            check = ui._register_validator(
                frame, lambda proposed: UiWorker._validate_reconnect_channel(
                    None, proposed)
            )
            box = tk.Spinbox(frame, from_=1, to=60, validate="key",
                             validatecommand=check)
            box.delete(0, "end")                     # the box starts at from_ (= 1)
            box.insert(0, "6")
            self.assertEqual(box.get(), "6")
            box.insert("end", "1")                   # 61 is not a channel
            self.assertEqual(box.get(), "6", "Tk must refuse the 61 keypress")
            box.delete(0, "end")
            box.insert(0, "12")
            self.assertEqual(box.get(), "12")
        finally:
            root.destroy()


class LayoutFileTests(unittest.TestCase):
    """The geometry can be calibrated once and reused (reconnect_layout.json)."""

    def test_a_calibrated_layout_is_loaded(self):
        import json
        import tempfile

        from reconnect_worker import load_layout

        data = {
            "reference_client": [1366, 768],
            "world_rows": [[500, 200], [500, 240], [500, 280], [500, 320], [500, 360]],
            "first_channel_centre": [300, 300],
            "channel_pitch": [50, 45],
            "channels_per_row": 5,
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "layout.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            layout = load_layout(path)
        self.assertEqual(layout.world_first_row_centre, (500, 200))
        self.assertEqual(layout.world_row_pitch, 40)
        self.assertEqual(layout.first_channel_centre, (300, 300))
        self.assertEqual(layout.channel_pitch, (50, 45))
        self.assertEqual(layout.channels_per_row, 5)
        # and it behaves like the built-in layout geometry
        self.assertEqual(layout.channel_key_moves(6), ["down"])
        self.assertEqual(layout.world_click_point(4, (1366, 768)), (500, 360))

    def test_a_missing_or_broken_file_falls_back_to_the_defaults(self):
        from reconnect_worker import load_layout

        self.assertIsNotNone(load_layout(Path("no/such/layout.json")))
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "layout.json"
            path.write_text("{ this is not json", encoding="utf-8")
            self.assertIsNotNone(load_layout(path))
            path.write_text('{"world_rows": [[1, 2]]}', encoding="utf-8")
            self.assertIsNotNone(load_layout(path))


class LoginReferenceAssetTests(unittest.TestCase):
    """The worker's own reference file: it must be read as a COLOUR, not as a shape."""

    def test_the_operators_reference_gives_the_cream_base_colour(self):
        from reconnect_worker import load_login_reference

        reference = load_login_reference(
            Path(__file__).resolve().parent / "screenshots" / "login_page_target.jpg")
        self.assertIsNotNone(reference)
        # measured on the operator's own crop: warm cream, B < G < R
        blue, green, red = reference.base_bgr
        self.assertLess(blue, green)
        self.assertLess(green, red)
        self.assertGreaterEqual(blue, 180)
        for channel in range(3):
            self.assertLessEqual(reference.lower_bgr[channel],
                                 reference.base_bgr[channel])
            self.assertLessEqual(reference.base_bgr[channel],
                                 reference.upper_bgr[channel])

    def test_a_frame_without_the_colour_stops_the_worker_before_any_key(self):
        sender = _FakeSender()
        clicker = _FakeClicker()
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=32),
            layout=LAYOUT,
            capture_fn=lambda: (np.zeros((768, 1366, 3), dtype=np.uint8),
                                (0, 0, 1366, 768)),
            clicker=clicker,
            template_path=Path(__file__).resolve().parent / "screenshots"
            / "login_page_target.jpg",
            sleep=lambda seconds: None,
        )
        worker.set_enabled(True)
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        self.assertEqual(clicker.clicks, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        self.assertTrue(any(state == "failed" for state, _detail in reports), reports)

    def test_a_missing_reference_stops_the_worker_with_a_clear_report(self):
        sender = _FakeSender()
        worker = ReconnectWorker(
            sender,
            threading.Event(),
            queue.Queue(maxsize=32),
            layout=LAYOUT,
            capture_fn=lambda: (np.zeros((768, 1366, 3), dtype=np.uint8),
                                (0, 0, 1366, 768)),
            clicker=_FakeClicker(),
            template_path=Path("does/not/exist.jpg"),
            sleep=lambda seconds: None,
        )
        worker.set_enabled(True)
        worker.notify_disconnect()
        worker._handle_disconnect()
        self.assertEqual(sender.keys, [])
        reports = []
        while not worker.result_queue.empty():
            reports.append(worker.result_queue.get_nowait())
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(any("颜色参考图" in detail for detail in failed), reports)


if __name__ == "__main__":
    unittest.main()
