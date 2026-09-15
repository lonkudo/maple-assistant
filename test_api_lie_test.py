# -*- coding: utf-8 -*-
"""测试api: the panel's API drill, exercised against the local mimic backend.

No game window and no network are needed: the worker takes a capture function, so a synthetic
frame stands in for the screenshot and the mimic stands in for the service.  That keeps the
whole chain under test - crop -> 372x248 -> jpeg90 -> base64 -> WebSocket -> frame_result ->
reverse conversion to client/screen pixels - and the pace (5 fps) is measured, not assumed.
"""

import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from api_lie_test import ApiLieTestWorker, TEST_KEY  # noqa: E402
from autolie_api.endpoints import Endpoint, load_endpoints, ws_endpoints  # noqa: E402
from autolie_api.intergration import lie_roi_box  # noqa: E402

CLIENT = (1366, 768)
WINDOW_RECT = (528, 255, 528 + CLIENT[0], 255 + CLIENT[1])


def game_frame(*, marker=(700, 400)) -> np.ndarray:
    """A synthetic client frame with a bright target inside the ROI."""

    frame = np.full((CLIENT[1], CLIENT[0], 3), 40, dtype=np.uint8)
    left, top, width, height = lie_roi_box(*CLIENT)
    frame[top:top + height, left:left + width] = (120, 90, 60)
    cv2.circle(frame, marker, 9, (255, 255, 255), -1)
    return frame


class _FakeSender:
    def __init__(self, *, selected=True):
        self.selected = 0
        self._selected = selected

    def select_window(self):
        self.selected += 1
        return self._selected


class KeyStoreTests(unittest.TestCase):
    """The key comes from the panel, the environment or the vendor's file - never logged whole."""

    def test_the_key_file_is_read_and_its_note_ignored(self):
        from autolie_api.key_store import read_key_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_secret.txt"
            path.write_text("tw-abc12345-67890 // do not share, bound to one machine\n",
                            encoding="utf-8")
            self.assertEqual(read_key_file(path), "tw-abc12345-67890")
            path.write_text("no key here\n", encoding="utf-8")
            self.assertEqual(read_key_file(path), "")
            self.assertEqual(read_key_file(Path("does/not/exist.txt")), "")

    def test_the_operators_own_file_yields_a_key(self):
        from autolie_api.key_store import KEY_FILE, read_key_file

        if not KEY_FILE.is_file():
            self.skipTest("no key_secret.txt in this checkout")
        key = read_key_file()
        self.assertTrue(key, "the operator's key file should give a key")
        self.assertNotIn(" ", key, "a key is a single token")

    def test_the_resolution_order_is_panel_then_environment_then_file(self):
        import os

        from autolie_api.key_store import load_product_key

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_secret.txt"
            path.write_text("file-key-1234\n", encoding="utf-8")
            self.assertEqual(load_product_key("panel-key-9999", path)[0], "panel-key-9999")
            saved = os.environ.get("LIE_PRODUCT_KEY")
            try:
                os.environ["LIE_PRODUCT_KEY"] = "env-key-1234"
                self.assertEqual(load_product_key("", path), ("env-key-1234", "LIE_PRODUCT_KEY"))
                del os.environ["LIE_PRODUCT_KEY"]
                self.assertEqual(load_product_key("", path), ("file-key-1234", "key_secret.txt"))
                os.environ.pop("LIE_PRODUCT_KEY", None)
                self.assertEqual(load_product_key("", Path("does/not/exist.txt")), ("", "无"))
            finally:
                if saved is not None:
                    os.environ["LIE_PRODUCT_KEY"] = saved
                else:
                    os.environ.pop("LIE_PRODUCT_KEY", None)

    def test_the_mask_never_shows_the_whole_key(self):
        from autolie_api.key_store import mask_key

        key = "tw-abc12345-67890"
        masked = mask_key(key)
        self.assertNotIn(key, masked)
        self.assertIn("tw-abc", masked)
        self.assertEqual(mask_key(""), "(未设置)")


class WorkerTests(unittest.TestCase):
    def _worker(self, **kwargs):
        self.results: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=512)
        self.tmp = tempfile.TemporaryDirectory()
        defaults = dict(
            results=self.results,
            stop_event=threading.Event(),
            key_sender=_FakeSender(),
            capture_fn=lambda: (game_frame(), WINDOW_RECT),
            out_dir=Path(self.tmp.name),
            fps=5.0,
            duration=1.0,
            # tests never touch the real service: the operator's key file must not be used here
            use_mimic=True,
        )
        defaults.update(kwargs)
        worker = ApiLieTestWorker(**defaults)
        self.addCleanup(self.tmp.cleanup)
        return worker

    @staticmethod
    def _drain(results):
        items = []
        while not results.empty():
            items.append(results.get_nowait())
        return items

    def test_one_press_runs_the_whole_chain_against_the_mimic(self):
        worker = self._worker()
        started = time.perf_counter()
        worker.run()                                   # run in-thread: deterministic
        elapsed = time.perf_counter() - started
        reports = self._drain(self.results)
        states = [state for state, _detail in reports]

        self.assertIn("backend", states)               # which backend was chosen
        self.assertIn("start", states)
        self.assertIn("done", states)
        self.assertNotIn("failed", states, reports)
        backend = [detail for state, detail in reports if state == "backend"][-1]
        self.assertIn("本地模拟后端", backend, "no key -> the local mimic is used")

        frames = [detail for state, detail in reports if state == "frame"]
        self.assertEqual(len(frames), 5, "1 second at 5 fps")
        self.assertTrue(all("screen=(" in line for line in frames), frames)
        self.assertTrue(all("quota=" in line for line in frames), frames)
        self.assertGreaterEqual(elapsed, 0.9, "the drill keeps the 5 fps pace")

        self.assertEqual(worker.stats.frames, 5)
        self.assertEqual(worker.stats.results, 5)
        self.assertEqual(worker.stats.failures, 0)
        self.assertIsNotNone(worker.stats.last_screen)
        self.assertEqual(worker.stats.holds, 0, "5 frames, the mimic holds every 7th")
        self.assertGreater(worker.stats.bytes_sent, 0)

        summary = [detail for state, detail in reports if state == "done"][-1]
        self.assertIn("frames=5", summary)
        self.assertIn("quota_left=", summary)

    def test_the_answered_position_is_mapped_onto_the_target(self):
        """The mimic answers the bright blob; the mapped screen point must land on it."""

        marker = (700, 400)
        screens: list[tuple[float, float]] = []
        worker = self._worker(capture_fn=lambda: (game_frame(marker=marker), WINDOW_RECT),
                              duration=0.4)
        worker.run()
        for line in [detail for state, detail in self._drain(self.results)
                     if state == "frame"]:
            text = line.split("screen=(")[1].split(")")[0]
            screens.append((float(text.split(",")[0]), float(text.split(",")[1])))
        self.assertTrue(screens)
        for screen_x, screen_y in screens:
            # client pixel = screen - window origin; the blob is at the marker
            self.assertAlmostEqual(screen_x - WINDOW_RECT[0], marker[0], delta=4.0)
            self.assertAlmostEqual(screen_y - WINDOW_RECT[1], marker[1], delta=4.0)

    def test_annotated_frames_are_written(self):
        worker = self._worker(duration=1.0)
        worker.run()
        captures = [detail for state, detail in self._drain(self.results)
                    if state == "capture"]
        self.assertTrue(captures, "at least one annotated frame per second")
        for path in captures:
            self.assertTrue(str(path).endswith(".jpg"), path)
            self.assertTrue(Path(path).is_file(), path)

    def test_a_missing_game_window_stops_with_a_clear_message(self):
        worker = self._worker(key_sender=_FakeSender(selected=False))
        worker.run()
        reports = self._drain(self.results)
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(failed)
        self.assertIn("找不到游戏窗口", failed[0])
        self.assertEqual(worker.stats.frames, 0)

    def test_a_frame_that_cannot_be_captured_is_reported(self):
        worker = self._worker(capture_fn=lambda: (None, (0, 0, 0, 0)))
        worker.run()
        failed = [detail for state, detail in self._drain(self.results)
                  if state == "failed"]
        self.assertTrue(any("无法截取游戏窗口" in detail for detail in failed), failed)

    def test_stopping_mid_drill_is_honoured(self):
        worker = self._worker(duration=10.0)           # long, but stopped after ~0.3s
        thread = threading.Thread(target=worker.run)
        thread.start()
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            time.sleep(0.01)
        worker.request_stop()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "the drill must stop")
        states = [state for state, _detail in self._drain(self.results)]
        self.assertIn("stopped", states)
        self.assertLess(worker.stats.frames, 20, "it stopped early")

    def test_a_second_press_while_running_is_refused(self):
        worker = self._worker(duration=0.5)
        thread = threading.Thread(target=worker.run)
        thread.start()
        time.sleep(0.1)
        self.assertTrue(worker.running)
        worker.run()                                   # second press, same instance
        thread.join(timeout=5.0)
        failed = [detail for state, detail in self._drain(self.results)
                  if state == "failed"]
        self.assertTrue(any("测试已在运行" in detail for detail in failed), failed)

    def test_the_drill_uses_the_real_backend_when_a_key_is_configured(self):
        """With a key (and the mimic override off) a dead host must fail with the probe result."""

        worker = self._worker(key="LIE-FAKE-KEY", host="127.0.0.1", ports=(9,),
                              key_sender=None, duration=0.2, use_mimic=False)
        worker.run()
        reports = self._drain(self.results)
        failed = [detail for state, detail in reports if state == "failed"]
        self.assertTrue(failed, reports)
        self.assertIn("没有响应", failed[0])
        self.assertNotIn("本地模拟后端",
                         [detail for state, detail in reports if state == "backend"])


class EndpointTests(unittest.TestCase):
    """The endpoints come from the vendor file, unmodified."""

    def test_the_vendor_file_is_parsed(self):
        endpoints = load_endpoints()
        self.assertEqual([item.port for item in ws_endpoints()], [8001, 8002, 8003])
        http = [item.port for item in endpoints if item.transport == "http"]
        self.assertEqual(http, [8004, 8005, 8006])
        self.assertTrue(all(item.host for item in endpoints))
        self.assertEqual(ws_endpoints()[0].url, "ws://117.50.223.113:8001")

    def test_a_missing_file_falls_back_to_the_defaults(self):
        endpoints = load_endpoints(Path("does/not/exist.txt"))
        self.assertEqual([item.port for item in endpoints if item.transport == "ws"],
                         [8001, 8002, 8003])

    def test_a_written_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ip_port.txt"
            path.write_text("10.0.0.5:9001-9002 //ws\n10.0.0.5:9100\n", encoding="utf-8")
            endpoints = load_endpoints(path)
            self.assertEqual([(e.host, e.port, e.transport) for e in endpoints],
                             [("10.0.0.5", 9001, "ws"), ("10.0.0.5", 9002, "ws"),
                              ("10.0.0.5", 9100, "http")])
            self.assertEqual(Endpoint("10.0.0.5", 9100, "http").url, "http://10.0.0.5:9100")


class UiRowTests(unittest.TestCase):
    """The panel row: one 测试api button that plays a video.  No 时长 box, no 密钥 button."""

    def test_the_panel_has_only_the_video_button_and_no_settings(self):
        """The redundant controls are gone: one button, and no length/key widgets at all."""

        source = Path("ui_worker.py").read_text(encoding="utf-8")
        self.assertIn('text="测试api"', source)
        for gone in ("测试api（抓屏）", "_api_test_seconds_box", "_api_test_seconds_var",
                     "_api_test_key_button", "_api_test_change_key",
                     "_api_test_screen_clicked", "_load_api_test_settings",
                     "api_test_seconds", "api_lie_key"):
            self.assertNotIn(gone, source, gone)
        # the source is also free of the removed local lie pass
        for gone in ("auto_lie_worker", "_lie_demo", "_tracker_runtime"):
            self.assertNotIn(gone, source, gone)
        # the status line still says which backend will answer
        self.assertIn("_api_test_backend_text", source)

    def test_the_video_button_reports_a_failure_into_error_log(self):
        from ui_worker import UiWorker

        class _Worker:
            running = True

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                pass

        class _Status:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", "")

        class _Button:
            state = "normal"

            def configure(self, **kwargs):
                self.state = kwargs.get("state", self.state)

        class _Window:
            def __init__(self, *args, **kwargs):
                pass

            def update_frame(self, frame, hud=""):
                pass

            def image_region(self):
                return (10, 20, 1376, 788)

            def is_focused(self):
                return True

            def focus(self):
                pass

            def close(self):
                pass

        class _Ui(UiWorker):
            def __init__(self):
                self.api_test_results = queue.Queue()
                self.api_test_video_factory = lambda **kwargs: _Worker(**kwargs)
                self.api_test_window_factory = lambda *args, **kwargs: _Window()
                self.api_test_worker = None
                self.api_test_window = None
                self.api_test_video = ""
                self._api_test_display = queue.Queue(maxsize=2)
                self._api_test_status = _Status()
                self._api_test_button = _Button()
                self._api_test_key = ""
                self._api_test_video_dir = ""
                self._root = None
                self.error_log_blocks = []

            def _ask_api_test_video(self):
                return Path("C:/clips/lie.mp4")

            def _play_action_sound(self, success):
                pass

            def _save_api_test_settings(self):
                pass

            def _append_error_log(self, text, tag):
                self.error_log_blocks.append((tag, text))

        ui = _Ui()
        ui._api_test_clicked()
        ui.api_test_results.put(("failed", "所有端口都没有响应"))
        ui._drain_api_test_results()
        self.assertIn("失败", ui._api_test_status.text)
        self.assertEqual(len(ui.error_log_blocks), 1)
        self.assertIn("所有端口都没有响应", ui.error_log_blocks[0][1])
        self.assertEqual(ui._api_test_button.state, "normal")

    def test_the_status_line_says_which_backend_will_be_used(self):
        from unittest import mock

        from ui_worker import UiWorker

        class _Ui(UiWorker):
            def __init__(self, key):
                self._api_test_key = key
                self.api_test_video = ""

        ui = _Ui("")
        with mock.patch("autolie_api.key_store.load_product_key",
                        return_value=("", "无")):
            self.assertIn("本地模拟后端", ui._api_test_status_text())
        with mock.patch("autolie_api.key_store.load_product_key",
                        return_value=("LIE-KEY", "面板")):
            text = ui._api_test_status_text()
        self.assertIn("真实后端", text)
        # the source is named, the key itself never is
        self.assertIn("面板", text)
        self.assertNotIn("LIE-KEY", text)
        with mock.patch("autolie_api.key_store.load_product_key",
                        return_value=("LIE-FILE", "key_secret.txt")):
            self.assertIn("key_secret.txt", ui._api_test_status_text())
        # the chosen video is remembered in the line
        ui.api_test_video = "C:/clips/lie_20260910_155856.mp4"
        with mock.patch("autolie_api.key_store.load_product_key",
                        return_value=("", "无")):
            self.assertIn("lie_20260910_155856.mp4", ui._api_test_status_text())

    def test_the_video_button_plays_the_chosen_video_in_its_own_window(self):
        """测试api must ask for a video file - never grab the game window."""

        from ui_worker import API_TEST_VIDEO_SECONDS, UiWorker

        picked = Path("C:/clips/my_test_clip.mp4")

        class _Worker:
            running = False

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.image_rect = None
                self.started = False

            def start(self):
                self.started = True

            def request_stop(self):
                pass

            def set_paused(self, paused):
                pass

            def set_mouse_enabled(self, enabled):
                pass

        class _Window:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.frames = []
                self.closed = False
                self.focused = 0

            def update_frame(self, frame, hud=""):
                self.frames.append(hud)

            def image_region(self):
                return (100, 200, 1466, 968)

            def is_focused(self):
                return True

            def focus(self):
                self.focused += 1

            def close(self):
                self.closed = True

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

        windows: list[_Window] = []

        def _make_window(*args, **kwargs):
            window = _Window(*args, **kwargs)
            windows.append(window)
            return window

        class _Ui(UiWorker):
            def __init__(self):
                self.api_test_results = queue.Queue()
                self.api_test_factory = lambda **kwargs: _Worker(**kwargs)
                self.api_test_video_factory = lambda **kwargs: _Worker(**kwargs)
                self.api_test_window_factory = _make_window
                self.api_test_worker = None
                self.api_test_window = None
                self.api_test_video = ""
                self._api_test_display = queue.Queue(maxsize=2)
                self._api_test_status = _Status()
                self._api_test_button = _Button()
                self._api_test_key = ""
                self._api_test_video_dir = ""
                self._root = None
                self.error_log_blocks = []

            def _ask_api_test_video(self):
                return picked

            def _play_action_sound(self, success):
                pass

            def _save_api_test_settings(self):
                pass

            def _append_error_log(self, text, tag):
                self.error_log_blocks.append((tag, text))

        ui = _Ui()
        ui._api_test_clicked()
        worker = ui.api_test_worker
        self.assertIsNotNone(worker, "the video drill must be created")
        self.assertTrue(worker.started)
        self.assertEqual(Path(worker.kwargs["video"]), picked)
        self.assertEqual(worker.kwargs["key"], "")
        # the window shows the file name and warns that the mouse stays in the picture
        self.assertIn("my_test_clip.mp4", windows[0].args[1])
        self.assertIn("5 fps", ui._api_test_status.text)
        self.assertEqual(ui._api_test_button.state, "disabled")
        self.assertEqual(worker.kwargs["seconds"], API_TEST_VIDEO_SECONDS)

        # a displayed frame publishes the picture rectangle the mouse must stay inside
        ui._api_test_display.put((np.zeros((20, 30, 3), dtype=np.uint8), "帧 1/150"))
        ui._drain_api_test_results()
        self.assertEqual(worker.image_rect, (100, 200, 1466, 968))
        self.assertEqual(windows[0].frames, ["帧 1/150"])

        ui.api_test_results.put(("done", "sent=150 answered=147 rounds=4 reconnects=3"))
        ui._drain_api_test_results()
        self.assertTrue(windows[0].closed, "the video window must close when the drill ends")
        self.assertEqual(ui._api_test_button.state, "normal")
        self.assertIn("rounds=4", ui._api_test_status.text)

    def test_cancelling_the_video_picker_starts_nothing(self):
        from ui_worker import UiWorker

        started: list[int] = []

        class _Ui(UiWorker):
            def __init__(self):
                self.api_test_worker = None
                self.api_test_video_factory = lambda **kwargs: started.append(1)
                self._api_test_status = type("S", (), {
                    "configure": lambda _s, **kwargs: setattr(_s, "text",
                                                             kwargs.get("text", ""))})()

            def _ask_api_test_video(self):
                return None

        ui = _Ui()
        ui._api_test_clicked()
        self.assertEqual(started, [])
        self.assertIsNone(ui.api_test_worker)

    def test_the_run_length_is_fixed_with_no_panel_setting(self):
        """There is no 时长 box any more: the drill always uses the measured ~30s."""

        from api_lie_video import DEFAULT_SECONDS
        from ui_worker import API_TEST_VIDEO_SECONDS, UiWorker

        class _Ui(UiWorker):
            def __init__(self):
                pass

        self.assertEqual(_Ui()._api_test_video_seconds(), API_TEST_VIDEO_SECONDS)
        self.assertEqual(API_TEST_VIDEO_SECONDS, 30.0)
        # the worker's own default must agree with the panel's constant
        self.assertEqual(DEFAULT_SECONDS, API_TEST_VIDEO_SECONDS)


if __name__ == "__main__":
    unittest.main()
