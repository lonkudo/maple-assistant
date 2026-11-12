"""试跑 waits for a cold-starting 本地测谎 service instead of cancelling itself.

Field case (work/assistant.log 21:10): the operator pressed 试跑 while the supervisor
was still launching the service (started at 21:10:28, healthy at 21:10:35), and the
drill was cancelled with "本地测谎服务还没准备好，请等几秒再试".  These tests pin the
new behaviour: keep the request pending, poll /v1/health from a Tk timer, start the
run when the service answers, and only fail after the 40 s budget.
"""

import time
import unittest
from unittest.mock import patch

from ui_worker import UiWorker


class _Root:
    """Minimal Tk stand-in: records ``after`` jobs so a test can run them."""

    def __init__(self) -> None:
        self.jobs: list[tuple[int, object]] = []
        self.cancelled: list[object] = []

    def after(self, delay_ms, callback):
        self.jobs.append((delay_ms, callback))
        return f"job{len(self.jobs)}"

    def after_cancel(self, job):
        self.cancelled.append(job)


class ApiTestWaitTests(unittest.TestCase):
    def _worker(self, *, local: bool = True) -> UiWorker:
        worker = UiWorker.__new__(UiWorker)
        worker._root = _Root()
        worker._auto_lie_backend_is_local = lambda: local          # type: ignore[method-assign]
        worker._api_test_pending = None
        worker._api_test_wait_started_at = 0.0
        worker._api_test_wait_job = None
        worker._local_lie_ready = None
        worker._local_lie_note = ""
        worker.statuses = []
        worker.failures = []
        worker._set_api_test_status = lambda text: worker.statuses.append(text)  # type: ignore[method-assign]
        worker._report_api_test_failure = lambda text: worker.failures.append(text)  # type: ignore[method-assign]
        worker._ensure_local_lie_service = lambda timeout=5.0: (False, "127.0.0.1:8765 未响应")  # type: ignore[method-assign]
        worker.began = []
        worker._begin_api_test = lambda video, seconds: worker.began.append((video, seconds))  # type: ignore[method-assign]
        return worker

    def test_not_ready_keeps_the_request_pending_and_polls(self) -> None:
        worker = self._worker()
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        self.assertEqual(worker.began, [])
        self.assertEqual(worker._api_test_pending, ("drill.mp4", 5.0))
        self.assertGreater(worker._api_test_wait_started_at, 0.0)
        self.assertTrue(any("正在启动" in text for text in worker.statuses), worker.statuses)
        self.assertEqual(len(worker._root.jobs), 1)
        self.assertEqual(worker._root.jobs[0][0], 700)

    def test_ready_service_runs_the_drill_immediately(self) -> None:
        worker = self._worker()
        worker._ensure_local_lie_service = lambda timeout=5.0: (True, "ready")  # type: ignore[method-assign]
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        self.assertEqual(worker.began, [("drill.mp4", 5.0)])
        self.assertIsNone(worker._api_test_pending)
        self.assertEqual(worker._root.jobs, [])

    def test_remote_backend_never_waits(self) -> None:
        worker = self._worker(local=False)
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        self.assertEqual(worker.began, [("drill.mp4", 5.0)])
        self.assertIsNone(worker._api_test_pending)

    def test_tick_starts_the_waiting_drill_once_health_answers(self) -> None:
        worker = self._worker()
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        self.assertEqual(worker.began, [])
        # The probe now succeeds: the pending drill must start on the Tk thread.
        worker._ensure_local_lie_service = lambda timeout=5.0: (True, "ready")  # type: ignore[method-assign]
        with patch("local_lie_backend.probe_local_lie_service",
                   return_value=(True, "http://127.0.0.1:8765 已连接")):
            worker._api_test_wait_tick()
        self.assertEqual(worker.began, [("drill.mp4", 5.0)])
        self.assertIsNone(worker._api_test_pending)
        self.assertTrue(worker._local_lie_ready)

    def test_tick_keeps_polling_while_the_service_is_still_cold(self) -> None:
        worker = self._worker()
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        before = len(worker._root.jobs)
        with patch("local_lie_backend.probe_local_lie_service",
                   return_value=(False, "not yet")):
            worker._api_test_wait_tick()
        self.assertEqual(worker.began, [])
        self.assertEqual(worker._api_test_pending, ("drill.mp4", 5.0))
        self.assertGreater(len(worker._root.jobs), before)

    def test_tick_gives_up_after_the_budget(self) -> None:
        worker = self._worker()
        worker._api_test_begin_or_wait("drill.mp4", 5.0)
        worker._api_test_wait_started_at = time.monotonic() - 41.0
        with patch("local_lie_backend.probe_local_lie_service",
                   return_value=(False, "not yet")):
            worker._api_test_wait_tick()
        self.assertEqual(worker.began, [])
        self.assertIsNone(worker._api_test_pending)
        self.assertTrue(any("40 秒" in text for text in worker.failures), worker.failures)

    def test_a_second_press_does_not_restart_the_budget(self) -> None:
        worker = self._worker()
        worker._api_test_begin_or_wait("first.mp4", 5.0)
        started = worker._api_test_wait_started_at
        worker._api_test_begin_or_wait("second.mp4", 5.0)
        self.assertEqual(worker._api_test_wait_started_at, started)
        self.assertEqual(worker._api_test_pending, ("first.mp4", 5.0))


class ApiTestBodyTests(unittest.TestCase):
    """``_begin_api_test`` must run the whole body (a split once lost ``factory``)."""

    def _worker(self):
        worker = UiWorker.__new__(UiWorker)
        created: dict = {}

        class FakeWindow:
            def __init__(self, *args, **kwargs):
                created["window_kwargs"] = kwargs

            def image_region(self):
                return (0, 0, 10, 10)

            def close(self):
                created["closed"] = True

        class FakeDrill:
            running = False

            def __init__(self, **kwargs):
                created["drill_kwargs"] = kwargs
                self.image_rect = None
                self.started = False

            def start(self):
                self.started = True

        def drill_factory(**kwargs):
            return FakeDrill(**kwargs)

        worker._root = object()
        worker.api_test_video_factory = drill_factory
        worker.api_test_window_factory = FakeWindow
        worker.api_test_results = object()
        worker._auto_lie_backend_is_local = lambda: False            # type: ignore[method-assign]
        worker._ensure_local_lie_service = lambda timeout=5.0: (True, "ready")  # type: ignore[method-assign]
        worker._api_test_window_closed = lambda: None
        worker._api_test_toggle_mouse = lambda: None
        worker._api_test_toggle_pause = lambda: None
        worker._api_test_set_buttons = lambda state: None
        worker._api_test_backend_text = lambda: "远程" 
        worker._api_test_keep_focus = lambda: None
        worker._set_api_test_status = lambda text: None
        worker.failures = []
        worker._report_api_test_failure = lambda text: worker.failures.append(text)  # type: ignore[method-assign]
        worker.created = created
        return worker

    def test_remote_drill_is_created_and_started(self) -> None:
        worker = self._worker()

        class Video:
            name = "drill.mp4"

            def __str__(self):
                return "drill.mp4"

        with patch("ui_worker.get_server_secret", return_value="secret-123"):
            worker._begin_api_test(Video(), 7.0)

        self.assertEqual(worker.failures, [])
        drill = worker.api_test_worker
        self.assertTrue(drill.started)
        self.assertEqual(worker.created["drill_kwargs"]["seconds"], 7.0)
        self.assertEqual(worker.created["drill_kwargs"]["key"], "secret-123")
        self.assertFalse(worker.created["drill_kwargs"].get("backend"))
        self.assertEqual(drill.image_rect, (0, 0, 10, 10))
        self.assertEqual(worker.api_test_video, "drill.mp4")

    def test_missing_factory_reports_instead_of_raising(self) -> None:
        worker = self._worker()
        worker.api_test_video_factory = None

        class Video:
            name = "drill.mp4"

            def __str__(self):
                return "drill.mp4"

        worker._begin_api_test(Video(), 5.0)      # must not raise NameError
        self.assertTrue(worker.failures, worker.failures)
        self.assertTrue(any("测试视频" in text for text in worker.failures), worker.failures)


if __name__ == "__main__":
    unittest.main()
