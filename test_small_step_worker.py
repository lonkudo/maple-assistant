import threading
import unittest
from unittest import mock

from small_step_worker import SmallStepWorker


class SmallStepWorkerTests(unittest.TestCase):
    def test_interval_has_three_second_minimum(self) -> None:
        worker = SmallStepWorker(threading.Event(), step_interval=0.2)
        self.assertEqual(worker.step_interval, 3.0)

    def test_delay_includes_configured_random_gap(self) -> None:
        worker = SmallStepWorker(
            threading.Event(), step_interval=5.0, step_jitter_seconds=.4
        )
        with mock.patch("small_step_worker.random.uniform", return_value=.25):
            self.assertEqual(worker.next_delay(), 5.25)

    def test_tick_requests_serialized_motion(self) -> None:
        class Arbiter:
            def __init__(self):
                self.requests = 0

            def request_micro_step(self):
                self.requests += 1
                return True

        arbiter = Arbiter()
        worker = SmallStepWorker(
            threading.Event(), motion_arbiter=arbiter
        )
        self.assertTrue(worker.motion_arbiter.request_micro_step())
        self.assertEqual(arbiter.requests, 1)


if __name__ == "__main__":
    unittest.main()
