"""随机休息 on the movement side: the pause gate, the resume, and the exemptions.

Kept apart from ``test_movement_worker`` (5k+ lines) so the rest contract is
readable in one place.
"""

import queue
import threading
import unittest

from movement_worker import MovementWorker, MinimapObservation, Point


class Sender:
    dry_run = True

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def key_down(self, key: str) -> bool:
        self.events.append(("down", key))
        return True

    def key_up(self, key: str) -> bool:
        self.events.append(("up", key))
        return True

    def press(self, key: str, duration: float = 0) -> bool:
        return True


def make_worker(**kwargs) -> MovementWorker:
    defaults = dict(
        important_positions={},
        patrol_enabled=True,
        automation_active_event=threading.Event(),
        climbing_active_event=threading.Event(),
        dropping_active_event=threading.Event(),
        moving_active_event=threading.Event(),
        reconnect_active_event=threading.Event(),
    )
    defaults.update(kwargs)
    worker = MovementWorker(
        queue.Queue(), Sender(), threading.Event(),
        **{key: value for key, value in defaults.items() if key != "sender"},
    )
    return worker


class RandomRestMovementTests(unittest.TestCase):
    def test_rest_pauses_movement_and_blocks_attack_for_its_duration(self) -> None:
        worker = make_worker()
        worker.automation_active_event.set()
        self.assertTrue(worker.request_rest(5.0))
        self.assertGreater(worker._rest_remaining(), 0.0)
        self.assertFalse(worker._patrol_input_allowed())
        # The attack workers read these events as "movement/action input active".
        self.assertTrue(worker.climbing_active_event.is_set())
        self.assertTrue(worker.dropping_active_event.is_set())
        self.assertEqual(worker.rest_pause_count, 1)

    def test_cancel_rest_hands_the_input_paths_back(self) -> None:
        worker = make_worker()
        worker.automation_active_event.set()
        worker.request_rest(30.0)
        worker.cancel_rest("test")
        self.assertEqual(worker._rest_remaining(), 0.0)
        self.assertTrue(worker._patrol_input_allowed())
        self.assertFalse(worker.climbing_active_event.is_set())
        self.assertFalse(worker.dropping_active_event.is_set())

    def test_rest_is_refused_when_patrol_is_not_running(self) -> None:
        worker = make_worker()
        worker.automation_active_event.clear()
        self.assertFalse(worker.request_rest(5.0))
        self.assertEqual(worker._rest_remaining(), 0.0)
        self.assertFalse(worker.climbing_active_event.is_set())

    def test_rest_is_refused_while_auto_reconnect_owns_the_input(self) -> None:
        worker = make_worker()
        worker.automation_active_event.set()
        worker.reconnect_active_event.set()
        self.assertFalse(worker.request_rest(5.0))
        self.assertEqual(worker._rest_remaining(), 0.0)
        self.assertFalse(worker.climbing_active_event.is_set())

    def test_rest_frames_never_count_as_a_stall(self) -> None:
        positions = {
            "layer1": {
                "layer_y": .70, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .70},
                "right_most_pos": {"x": .8, "y": .70},
            },
        }
        worker = make_worker(important_positions=positions)
        worker.automation_active_event.set()
        worker._route_layers = ["layer1"]
        self.assertTrue(worker.request_rest(30.0))
        observation = MinimapObservation(Point(.5, .70), None, .9, (0, 0, 1, 1))
        for _ in range(10):
            worker._rescue_stuck_check(observation, now=100.0)
        self.assertEqual(worker._rescue_stuck_frames, 0)
        self.assertIsNone(worker._rescue_last_pos)
        # Once the rest is over the same frames count again.
        worker.cancel_rest("test")
        for index in range(3):
            worker._rescue_stuck_check(observation, now=100.0 + index * 0.2)
        self.assertGreater(worker._rescue_stuck_frames, 0)


if __name__ == "__main__":
    unittest.main()
