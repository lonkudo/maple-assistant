"""A real lie window must be served by the local 测谎 service, not dropped.

``_service_api_auto_lie`` used a cached readiness verdict and gave up when it said
"not ready", which is how a real lie event was lost while the CT service was cold
starting or restarting.  These tests pin the new contract:

* the port is probed FRESH for every window (``force_probe=True``);
* while it is not listening the event stays pending, a start is requested, and the
  UI tick retries;
* the retry is bounded by ``AUTO_LIE_PENDING_MAX_SECONDS``;
* once the service answers, the pass is created with the local backend and started.
"""

import time
import unittest

import ui_worker
from ui_worker import AUTOLIE_BACKEND_LOCAL, UiWorker


class _Status:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, **kwargs) -> None:
        self.text = kwargs.get("text", self.text)


class _Worker:
    def __init__(self) -> None:
        self.started = False
        self.feed_hook = None
        self.ready_hook = None

    def set_feed_start_from_detection(self, value) -> None:
        self.feed_hook = value

    def set_on_backend_ready(self, value) -> None:
        self.ready_hook = value

    def start(self) -> None:
        self.started = True


class AutoLieLocalServiceTests(unittest.TestCase):
    def _worker(self, *, ready: bool, note: str = "http://127.0.0.1:8765 未响应"):
        worker = UiWorker.__new__(UiWorker)
        worker._api_auto_lie_pending = True
        worker._api_auto_lie_pending_since = time.monotonic()
        worker._api_auto_lie_last_pass_started = 0.0
        worker._api_auto_lie_runs = 0
        worker._api_auto_lie_wait_logged = False
        worker._api_lie_pass_worker = None
        worker._api_lie_pass_local_backend = None
        worker._api_test_status = _Status()
        worker._worker_is_running = lambda w: False            # type: ignore[method-assign]
        worker._auto_lie_backend_is_local = lambda: True       # type: ignore[method-assign]
        worker._pause_patrol_for_api_pass = lambda: None       # type: ignore[method-assign]
        worker._probes = []

        def ensure(**kwargs):
            worker._probes.append(kwargs)
            return (ready, note) if kwargs.get("force_probe") else (not ready, note)

        worker._ensure_local_lie_service = ensure            # type: ignore[method-assign]
        worker._start_requests = []
        worker._local_lie_request_start = (                  # type: ignore[method-assign]
            lambda *, reason="": worker._start_requests.append(reason) or True
        )
        worker.created = []

        def factory(**kwargs):
            worker.created.append(kwargs)
            return _Worker()

        worker.api_auto_lie_factory = factory
        return worker

    def test_probe_is_forced_fresh_for_every_window(self) -> None:
        worker = self._worker(ready=True)
        worker._service_api_auto_lie()
        self.assertEqual(worker._probes, [{"force_probe": True}])

    def test_not_listening_keeps_the_event_pending_and_requests_a_start(self) -> None:
        worker = self._worker(ready=False)
        worker._service_api_auto_lie()
        self.assertTrue(worker._api_auto_lie_pending)
        self.assertEqual(worker.created, [])
        self.assertEqual(worker._start_requests, ["a lie window is waiting"])
        self.assertTrue(worker._api_auto_lie_service_wait_logged)
        self.assertIn("启动中", worker._api_test_status.text)

    def test_a_second_tick_keeps_the_event_and_logs_only_once(self) -> None:
        worker = self._worker(ready=False)
        worker._service_api_auto_lie()
        worker._service_api_auto_lie()
        # The window is still pending and the operator is told once, not every tick.
        self.assertTrue(worker._api_auto_lie_pending)
        self.assertTrue(worker._api_auto_lie_service_wait_logged)
        self.assertEqual(worker.created, [])

    def test_start_requests_are_rate_limited(self) -> None:
        worker = self._worker(ready=False)
        worker._local_lie_request_start = UiWorker._local_lie_request_start.__get__(worker, UiWorker)
        worker._local_lie_start_requested_at = 0.0
        import local_lie_service

        original = local_lie_service.ensure_started
        local_lie_service.ensure_started = lambda root=None, **kw: (False, "still cold")
        try:
            self.assertTrue(worker._local_lie_request_start(reason="test"))
            self.assertFalse(worker._local_lie_request_start(reason="test"))
        finally:
            local_lie_service.ensure_started = original

    def test_service_answering_after_the_wait_serves_the_window_locally(self) -> None:
        worker = self._worker(ready=False)
        worker._service_api_auto_lie()          # not listening yet
        self.assertEqual(worker.created, [])
        # The port comes up on a later tick.
        worker._ensure_local_lie_service = lambda **kwargs: (True, "ready")  # type: ignore[method-assign]
        worker._service_api_auto_lie()
        self.assertEqual(len(worker.created), 1)
        self.assertEqual(worker.created[0], {"backend": AUTOLIE_BACKEND_LOCAL})
        self.assertFalse(worker._api_auto_lie_pending)
        self.assertTrue(worker._api_lie_pass_local_backend)
        self.assertTrue(worker._api_lie_pass_worker.started)

    def test_service_that_never_answers_is_dropped_after_the_budget(self) -> None:
        worker = self._worker(ready=False)
        worker._api_auto_lie_pending_since = (
            time.monotonic() - (ui_worker.AUTO_LIE_PENDING_MAX_SECONDS + 5.0)
        )
        worker._service_api_auto_lie()
        self.assertFalse(worker._api_auto_lie_pending)
        self.assertEqual(worker.created, [])

    @staticmethod
    def api_test_status_stub_started(worker: UiWorker) -> bool:
        started = getattr(worker._api_lie_pass_worker, "started", False)
        return bool(started)


if __name__ == "__main__":
    unittest.main()
