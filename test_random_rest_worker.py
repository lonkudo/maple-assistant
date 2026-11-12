"""随机休息 schedule tests: the 75-120 min gap, the 3-15 s break, and the pause.

The ranges are exercised with an injected RNG and a fake clock, and one live run
with tiny ranges proves the worker actually pauses and then resumes.
"""

import random
import threading
import unittest

from random_rest_worker import (
    GAP_SECONDS_RANGE,
    REST_SECONDS_RANGE,
    RandomRestWorker,
)


class FakeMovement:
    """Records the pause requests the worker makes."""

    def __init__(self) -> None:
        self.requests: list[float] = []
        self.resumes = 0
        self.accept = True

    def request_rest(self, seconds: float) -> bool:
        self.requests.append(float(seconds))
        return self.accept

    def cancel_rest(self, reason: str = "") -> None:
        self.resumes += 1


def make_worker(movement: FakeMovement, *, automation: bool = True,
                gap=(60.0, 60.0), rest=(2.0, 2.0), seed: int = 7):
    event = threading.Event()
    if automation:
        event.set()
    return RandomRestWorker(
        threading.Event(),
        request_rest=movement.request_rest,
        end_rest=movement.cancel_rest,
        automation_active_event=event,
        gap_seconds=gap,
        rest_seconds=rest,
        tick_seconds=0.01,
        rng=random.Random(seed),
    )


class RandomRestTests(unittest.TestCase):
    def test_default_ranges_match_the_operator_specification(self) -> None:
        self.assertEqual(GAP_SECONDS_RANGE, (75 * 60.0, 120 * 60.0))
        self.assertEqual(REST_SECONDS_RANGE, (3.0, 15.0))

    def test_draws_stay_inside_the_ranges(self) -> None:
        worker = RandomRestWorker(
            threading.Event(),
            request_rest=lambda seconds: True,
            rng=random.Random(1),
        )
        for _ in range(200):
            gap = worker.next_gap()
            rest = worker.next_rest()
            self.assertGreaterEqual(gap, 75 * 60.0)
            self.assertLessEqual(gap, 120 * 60.0)
            self.assertGreaterEqual(rest, 3.0)
            self.assertLessEqual(rest, 15.0)

    def test_nothing_happens_while_disabled(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement)
        worker.tick(now=0.0)
        worker.tick(now=10_000.0)
        self.assertEqual(movement.requests, [])
        self.assertIsNone(worker.seconds_until_break())

    def test_break_fires_only_after_the_gap(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement, gap=(100.0, 100.0))
        worker.set_enabled(True)
        worker.tick(now=0.0)                 # schedules 0 -> 100
        self.assertAlmostEqual(worker.seconds_until_break(now=0.0) or 0.0, 100.0)
        worker.tick(now=99.0)                # too early
        self.assertEqual(movement.requests, [])
        worker.tick(now=100.0)               # due
        self.assertEqual(movement.requests, [2.0])
        self.assertEqual(movement.resumes, 1)
        self.assertEqual(worker.rest_count, 1)

    def test_gap_restarts_after_each_break(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement, gap=(100.0, 100.0), rest=(0.5, 0.5))
        worker.set_enabled(True)
        worker.tick(now=0.0)
        worker.tick(now=100.0)
        self.assertEqual(len(movement.requests), 1)
        # A fresh gap is measured from after the break, not from the old one.
        worker.tick(now=101.0)
        self.assertAlmostEqual(worker.seconds_until_break(now=101.0) or 0.0, 100.0)
        worker.tick(now=201.0)
        self.assertEqual(len(movement.requests), 2)

    def test_idle_patrol_time_does_not_count_towards_the_gap(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement, gap=(100.0, 100.0), automation=False)
        worker.set_enabled(True)
        worker.tick(now=0.0)
        worker.tick(now=500.0)
        self.assertEqual(movement.requests, [])
        self.assertIsNone(worker.seconds_until_break())
        # Automation resumes: the schedule starts over, no instant break.
        worker.automation_active_event.set()
        worker.tick(now=600.0)
        worker.tick(now=650.0)
        self.assertEqual(movement.requests, [])

    def test_a_refused_pause_is_retried_without_eating_the_gap(self) -> None:
        movement = FakeMovement()
        movement.accept = False
        worker = make_worker(movement, gap=(100.0, 100.0))
        worker.set_enabled(True)
        worker.tick(now=0.0)
        worker.tick(now=100.0)
        self.assertEqual(movement.requests, [2.0])
        self.assertEqual(worker.rest_count, 0)
        self.assertEqual(movement.resumes, 0)      # nothing to resume
        # Retried soon (not after another full gap).
        worker.tick(now=105.0)
        self.assertEqual(movement.requests, [2.0])
        movement.accept = True
        worker.tick(now=112.0)
        self.assertEqual(len(movement.requests), 2)
        self.assertEqual(movement.resumes, 1)

    def test_disabling_mid_flight_stops_scheduling(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement, gap=(10.0, 10.0))
        worker.set_enabled(True)
        worker.tick(now=0.0)
        worker.set_enabled(False)
        worker.tick(now=50.0)
        self.assertEqual(movement.requests, [])
        self.assertIsNone(worker.seconds_until_break())

    def test_live_thread_pauses_then_resumes(self) -> None:
        movement = FakeMovement()
        worker = make_worker(movement, gap=(0.02, 0.02), rest=(0.5, 0.5))
        worker.set_enabled(True)
        worker.start()
        try:
            import time

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not movement.requests:
                time.sleep(0.01)
        finally:
            worker.stop_event.set()
            worker.join(1.0)
        self.assertEqual(len(movement.requests), 1)
        self.assertAlmostEqual(movement.requests[0], 0.5)
        self.assertGreaterEqual(movement.resumes, 1)


if __name__ == "__main__":
    unittest.main()
