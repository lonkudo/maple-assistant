import queue
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np
from PIL import Image

from capture_worker import CapturedFrame
from movement_worker import (
    MinimapObservation,
    MovementDecision,
    ClimbState,
    MovementWorker,
    Point,
    analyze_minimap,
    detect_marker,
    detect_layer_by_y,
    detect_layer_by_world_y,
    plan_movement,
    move_towards_rope,
    move_to_left_most,
    move_to_right_most,
    climb,
    preserve_persistent_climb,
    _send_tap,
    _drop_through_platform,
)


def diamond(image, cx, cy, radius=4):
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if abs(x) + abs(y) <= radius:
                image[cy + y, cx + x] = (255, 255, 136)


class MovementTests(unittest.TestCase):
    def test_rope_climb_triggers_inside_inner_gap_only(self):
        rope_x = .5
        in_band = MinimapObservation(
            Point(rope_x - .0220, .5), None, .9, (0, 0, 1, 1)
        )
        within_inner = MinimapObservation(
            Point(rope_x - .0210, .5), None, .9, (0, 0, 1, 1)
        )
        too_far = MinimapObservation(
            Point(rope_x - .0230, .5), None, .9, (0, 0, 1, 1)
        )

        band_plan = move_towards_rope(
            in_band, rope_x, .0229, inner_range=.0215
        )
        inner_plan = move_towards_rope(
            within_inner, rope_x, .0229, inner_range=.0215
        )
        far_plan = move_towards_rope(
            too_far, rope_x, .0229, inner_range=.0215
        )

        # Outside the inner gap the character walks toward the inner edge.
        self.assertEqual(band_plan.decision.key, "right")
        self.assertAlmostEqual(band_plan.target_x, rope_x - .0215, places=6)
        # Inside the inner gap the climb attempt happens immediately.
        self.assertEqual(inner_plan.decision.key, "jump_climb_right")
        self.assertEqual(far_plan.decision.key, "right")

    def test_horizontal_correction_cannot_cancel_attached_climb(self):
        state = ClimbState(phase="climbing-up", up_held=True)
        proposed = MovementDecision("right", "tiny rope-edge correction", .30)

        protected = preserve_persistent_climb(state, proposed)

        self.assertIsNone(protected.key)
        self.assertIn("Up remains held", protected.reason)

    def test_yolo_jump_decision_hands_climb_to_patrol(self):
        # Once the character is on the rope (climbing-up, Up held), even a
        # fresh YOLO jump decision must be converted to the patrol's held-Up
        # climb - YOLO only jumps, patrol climbs.
        state = ClimbState(phase="climbing-up", up_held=True)
        yolo_jump = MovementDecision(
            "jump_climb_right", "YOLO rope gap +60px; jump right", .08
        )

        protected = preserve_persistent_climb(state, yolo_jump)

        self.assertIsNone(protected.key)
        self.assertIn("Up remains held", protected.reason)

    def test_drop_is_simultaneous_alt_down_chord(self):
        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key):
                self.events.append(("down", key)); return True
            def key_up(self, key):
                self.events.append(("up", key)); return True
        sender = Sender()
        with patch("movement_worker.time.sleep") as sleep:
            self.assertTrue(_drop_through_platform(sender, .10))
        sleep.assert_called_once_with(.10)
        self.assertEqual(sender.events, [
            ("down", "down"), ("down", "alt"),
            ("up", "alt"), ("up", "down"),
        ])

    def test_on_first_layer_accepts_marker_below_recorded_y(self):
        # The drop landed a few pixels BELOW the recorded layer_y (0.685 vs
        # 0.662).  The first layer is the bottom: being at-or-below the band
        # must count as arrived, or the bot presses Alt+Down forever.
        positions = {
            "layer1": {"layer_y": 0.66185, "y_tolerance": 0.02,
                       "layer_world_y": 5.495099,
                       "world_y_tolerance": 0.75,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .56, "y_tolerance": .02,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
            final_layer_action="drop_to_first_layer", first_layer="layer1",
        )
        below = MinimapObservation(Point(.79, 0.684971), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._on_first_layer(below))
        # Still on an upper layer: not arrived.
        upper = MinimapObservation(Point(.79, .56), None, .9, (0, 0, 1, 1))
        self.assertFalse(worker._on_first_layer(upper))

    def test_on_first_layer_marker_y_wins_over_stale_world_y(self):
        # The marker is EXACTLY on layer1's recorded layer_y, but the world-Y
        # tracker is stale (still reading layer2's value).  The marker path
        # must win - a single world-Y path would never recognize arrival.
        positions = {
            "layer1": {"layer_y": 0.66185, "y_tolerance": 0.025,
                       "layer_world_y": 5.495099,
                       "world_y_tolerance": 0.75,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .552023, "y_tolerance": .025,
                       "layer_world_y": 2.461896,
                       "world_y_tolerance": 0.75,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
            final_layer_action="drop_to_first_layer", first_layer="layer1",
        )
        # Marker at layer1's y; world-Y still reports layer2's stale value.
        on_first = MinimapObservation(
            Point(.79, 0.66185), None, .9, (0, 0, 1, 1)
        )
        on_first = replace(
            on_first,
            world_y_diamonds=2.461896,   # stale layer2 reading
            structure_confidence=0.9,
        )
        self.assertTrue(worker._on_first_layer(on_first))
        # Both signals say upper layer: not arrived.
        upper = MinimapObservation(Point(.79, .552), None, .9, (0, 0, 1, 1))
        upper = replace(
            upper,
            world_y_diamonds=2.461896,
            structure_confidence=0.9,
        )
        self.assertFalse(worker._on_first_layer(upper))

    def test_final_layer_drops_instead_of_targeting_rope_then_resets(self):
        positions = {
            "layer1": {"layer_y": .70, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .56, "y_tolerance": .02,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
            final_layer_action="drop_to_first_layer", first_layer="layer1",
        )
        worker._route_layer_index = 1
        worker._route_phase = "rope"
        on_final = MinimapObservation(Point(.6, .56), None, .9, (0, 0, 1, 1))
        self.assertEqual(worker._route_target(on_final),
                         (None, False, "layer2.drop-to-first"))
        self.assertFalse(worker._on_first_layer(on_final))
        on_first = MinimapObservation(Point(.6, .70), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._on_first_layer(on_first))
        worker._reset_route_loop()
        self.assertEqual(worker._route_layer_index, 0)
        self.assertEqual(worker._route_phase, "left")

    def test_descending_flag_blocks_resync_hijack_and_clears_on_arrival(self):
        positions = {
            "layer1": {"layer_y": .70, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .56, "y_tolerance": .02,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
            "layer3": {"layer_y": .42, "y_tolerance": .02,
                       "left_most_pos": {"x": .25, "y": .42},
                       "right_most_pos": {"x": .65, "y": .42}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions,
            route_order=["layer1", "layer2", "layer3"],
            final_layer_action="drop_to_first_layer", first_layer="layer1",
        )
        worker._route_layer_index = 2  # on the final layer
        worker._route_phase = "rope"
        # Mid-descent: the marker is on layer2 (an intermediate platform).
        # Without the guard, _resync_route_layer would switch the route to
        # layer2 and restart patrol there - the bug.  The descent flag makes
        # run() skip resync, so the route stays pinned to the final layer.
        worker._descending_to_first = True
        mid_drop = MinimapObservation(Point(.6, .56), None, .9, (0, 0, 1, 1))
        # (run() skips _resync_route_layer while descending; the route index
        # must remain on the final layer and keep issuing the drop.)
        self.assertEqual(worker._route_layer_index, 2)
        self.assertEqual(worker._route_target(mid_drop),
                         (None, False, "layer3.drop-to-first"))
        # If resync ran anyway (e.g. flag cleared too early), it WOULD
        # hijack to layer2 - prove the guard matters.
        worker._descending_to_first = False
        worker._resync_route_layer(mid_drop)
        self.assertEqual(worker._route_layer_index, 1)
        # Arrival on the first layer resets the loop and clears the flag.
        worker._descending_to_first = True
        on_first = MinimapObservation(Point(.6, .70), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._on_first_layer(on_first))
        worker._reset_route_loop()
        self.assertFalse(worker._descending_to_first)
        self.assertEqual(worker._route_layer_index, 0)
        self.assertEqual(worker._route_phase, "left")

    def test_moving_event_follows_left_right_decisions(self):
        import threading

        moving = threading.Event()
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, moving_active_event=moving,
        )
        self.assertFalse(moving.is_set())
        # Walking decisions raise the flag.
        worker._update_moving_event(MovementDecision("left", "patrol"))
        self.assertTrue(moving.is_set())
        worker._update_moving_event(MovementDecision("right", "rope"))
        self.assertTrue(moving.is_set())
        # Idle/aligned/climb decisions lower it again.
        worker._update_moving_event(MovementDecision(None, "aligned"))
        self.assertFalse(moving.is_set())
        worker._update_moving_event(MovementDecision("climb", "rope"))
        self.assertFalse(moving.is_set())

    def test_yolo_rope_jump_gate(self):
        import tempfile
        from pathlib import Path
        from combat_coordination import RopeStateFile

        tmp = Path(tempfile.mkdtemp()) / "rope_state.json"
        state = RopeStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, rope_state_path=str(tmp),
            rope_jump_px=140.0,
        )
        # No state yet: fall back to the patrol (minimap) plan.
        self.assertIsNone(worker._yolo_rope_action())
        # Small real gap -> jump in the real direction (YOLO's only job).
        state.write(True, rope_x=1340.0, char_x=1280.0)
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_right")
        # Large real gap -> NO YOLO decision: patrol walks to the zone.
        state.write(True, rope_x=2000.0, char_x=1280.0)
        self.assertIsNone(worker._yolo_rope_action())
        # Rope not visible -> fall back to the patrol plan.
        state.write(False)
        self.assertIsNone(worker._yolo_rope_action())

    def test_yolo_on_rope_dead_zone_hands_climb_to_patrol(self):
        import tempfile
        from pathlib import Path
        from combat_coordination import RopeStateFile

        tmp = Path(tempfile.mkdtemp()) / "rope_state.json"
        state = RopeStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, rope_state_path=str(tmp),
            rope_jump_px=140.0, on_rope_px=50.0,
        )
        # Character overlays the rope (gap 30px) WHILE CLIMBING (Up held):
        # no-op decision, patrol keeps holding Up.
        worker._climb_state.up_held = True
        state.write(True, rope_x=1310.0, char_x=1280.0)
        decision = worker._yolo_rope_action()
        self.assertIsNotNone(decision)
        self.assertIsNone(decision.key)
        self.assertIn("on rope", decision.reason)
        # Idle character at the same gap must JUMP to grab the rope (the
        # original bug: idle + small gap waited forever at layer1).
        worker._climb_state.up_held = False
        worker._climb_state.phase = "idle"
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_right")
        # Just outside the dead zone while climbing (gap 60px): jump resumes.
        worker._climb_state.up_held = True
        state.write(True, rope_x=1340.0, char_x=1280.0)
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_right")

    def test_yolo_jump_direction_stable_for_tiny_gaps(self):
        import tempfile
        from pathlib import Path
        from combat_coordination import RopeStateFile

        tmp = Path(tempfile.mkdtemp()) / "rope_state.json"
        state = RopeStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, rope_state_path=str(tmp),
            rope_jump_px=140.0, on_rope_px=50.0,
        )
        # First jump right, then a tiny gap flips sign: direction must stay
        # right (no left/right thrash on box noise).
        state.write(True, rope_x=1340.0, char_x=1280.0)
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_right")
        state.write(True, rope_x=1283.0, char_x=1280.0)  # gap +3px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_right")
        state.write(True, rope_x=1277.0, char_x=1280.0)  # gap -3px (noise)
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_right")

    def test_patrol_busy_hysteresis_holds_after_idle_reset(self):
        import tempfile
        import time
        from pathlib import Path
        from combat_coordination import PatrolStateFile

        tmp = Path(tempfile.mkdtemp()) / "patrol_state.json"
        state = PatrolStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={},
            patrol_state_path=str(tmp),
            patrol_busy_hold=3.0,
        )
        # Emulate a stall reset to idle right after a climb: the hold window
        # (patrol_busy_until in the future) must keep the published state
        # busy so the YOLO attack stays blocked during the re-grab.
        worker._climb_state.phase = "idle"
        worker._patrol_busy_until = time.monotonic() + 3.0
        worker._patrol_state.write(True, "climb")
        self.assertTrue(state.is_busy())
        # After the hold window expires, a fresh idle frame publishes idle.
        worker._patrol_busy_until = 0.0
        worker._patrol_state.write(False, None)
        self.assertFalse(state.is_busy())

    def test_attack_state_pause_gate(self):
        import tempfile
        from pathlib import Path
        from combat_coordination import AttackStateFile

        tmp = Path(tempfile.mkdtemp()) / "attack_state.json"
        state = AttackStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, attack_state_path=str(tmp),
        )
        # No state file yet: patrol must NOT be paused.
        self.assertFalse(worker._attack_state.is_active())
        self.assertFalse(worker._attack_paused_last)
        # Attack worker reports a target: the gate trips.
        state.write(True, (100.0, 200.0))
        self.assertTrue(worker._attack_state.is_active())
        # Attack clears: the gate releases.
        state.write(False)
        self.assertFalse(worker._attack_state.is_active())

    def test_y_only_incomplete_layer_can_be_detected(self):
        layers = {
            "layer1": {"layer_y": .698864, "y_tolerance": .02},
            "layer2": {"layer_y": .565341, "y_tolerance": .02,
                       "calibration_status": "awaiting_left_and_right_positions"},
        }
        self.assertEqual(detect_layer_by_y(.565000, layers), "layer2")
        self.assertIsNone(detect_layer_by_y(.620000, layers))

    def test_fall_resyncs_to_detected_layer_patrol(self):
        class Sender:
            def __init__(self): self.released = []
            def key_up(self, key): self.released.append(key); return True

        positions = {
            "layer1": {"layer_y": .70, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .56, "y_tolerance": .02,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
        }
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
        )
        worker._route_layer_index = 1
        worker._route_phase = "rope"
        worker._climb_state = ClimbState(phase="climbing-up", up_held=True)
        fallen = MinimapObservation(Point(.5, .70), None, .9, (0, 0, 1, 1))

        self.assertEqual(worker._resync_route_layer(fallen), "layer1")
        self.assertEqual(worker._route_layer_index, 0)
        self.assertEqual(worker._route_phase, "left")
        self.assertEqual(worker._climb_state.phase, "idle")
        self.assertEqual(sender.released, ["up"])

    def test_return_to_first_layer_resets_world_y_for_next_loop(self):
        class Sender:
            def key_up(self, key): return True

        class Tracker:
            def __init__(self): self.anchors = []
            def start_session(self, world_y): self.anchors.append(world_y)

        positions = {
            "layer1": {
                "layer_world_y": -.4, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
            "layer2": {
                "layer_world_y": -7.4, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        tracker = Tracker()
        worker = MovementWorker(
            queue.Queue(), Sender(), threading.Event(),
            important_positions=positions,
            route_order=["layer1", "layer2"],
            first_layer="layer1",
            structure_tracker=tracker,
        )
        worker._route_layer_index = 1
        returned = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-.35, structure_confidence=.9,
        )

        self.assertEqual(worker._resync_route_layer(returned), "layer1")
        self.assertEqual(tracker.anchors, [-.4])
        self.assertEqual(worker._route_layer_index, 0)

    def test_stationary_patrol_pins_world_y_and_climb_reanchors_tracker(self):
        class Tracker:
            def __init__(self): self.anchors = []
            def reanchor_world_y(self, world_y): self.anchors.append(world_y)

        positions = {
            "layer1": {
                "layer_world_y": -.4,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        tracker = Tracker()
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions=positions,
            route_order=["layer1"],
            structure_tracker=tracker,
        )
        worker._route_layer_index = 0
        aliased = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=2.4, structure_confidence=.95,
        )

        pinned = worker._pin_stationary_layer_world_y(aliased)
        worker._reanchor_tracker_to_current_layer()

        self.assertEqual(pinned.world_y_diamonds, -.4)
        self.assertEqual(tracker.anchors, [-.4])

    def test_same_layer_does_not_restart_patrol_phase(self):
        positions = {
            "layer1": {"layer_y": .70, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1"],
        )
        worker._route_layer_index = 0
        worker._route_phase = "right"
        same = MinimapObservation(Point(.5, .699), None, .9, (0, 0, 1, 1))

        self.assertEqual(worker._resync_route_layer(same), "layer1")
        self.assertEqual(worker._route_phase, "right")

    def test_layer_resync_ignores_player_x(self):
        positions = {
            "layer1": {"layer_y": .70, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .70},
                       "right_most_pos": {"x": .8, "y": .70}},
            "layer2": {"layer_y": .56, "y_tolerance": .02,
                       "left_most_pos": {"x": .3, "y": .56},
                       "right_most_pos": {"x": .6, "y": .56}},
        }
        for player_x in (.0, .25, .50, .75, 1.0):
            worker = MovementWorker(
                queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
                important_positions=positions,
                route_order=["layer1", "layer2"],
            )
            observation = MinimapObservation(
                Point(player_x, .560), None, .9, (0, 0, 1, 1)
            )
            self.assertEqual(worker._resync_route_layer(observation), "layer2")
            self.assertEqual(worker._route_layer_index, 1)

    def test_left_and_right_boundary_movement_are_independent(self):
        observation = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        left = move_to_left_most(observation, Point(.2, .7))
        right = move_to_right_most(observation, Point(.8, .7))
        self.assertEqual((left.stage, left.decision.key, left.decision.duration),
                         ("move-to-left-most", "left", 2.0))
        self.assertEqual((right.stage, right.decision.key, right.decision.duration),
                         ("move-to-right-most", "right", 2.0))

    def test_boundary_functions_accept_crossing_without_correction(self):
        crossed_left = MinimapObservation(Point(.15, .7), None, .9, (0, 0, 1, 1))
        crossed_right = MinimapObservation(Point(.85, .7), None, .9, (0, 0, 1, 1))
        left = move_to_left_most(crossed_left, Point(.2, .7))
        right = move_to_right_most(crossed_right, Point(.8, .7))
        self.assertTrue(left.reached_or_crossed)
        self.assertTrue(right.reached_or_crossed)
        self.assertIsNone(left.decision.key)
        self.assertIsNone(right.decision.key)

    def test_move_towards_rope_has_distinct_travel_and_climb_stages(self):
        far = MinimapObservation(Point(.20, .70), None, .9, (0, 0, 1, 1))
        near = MinimapObservation(Point(.47, .70), None, .9, (0, 0, 1, 1))
        far_plan = move_towards_rope(far, .4926470588, .0325)
        near_plan = move_towards_rope(near, .4926470588, .0325)
        self.assertEqual(far_plan.stage, "move-to-rope-edge")
        self.assertEqual(far_plan.decision.key, "right")
        self.assertAlmostEqual(far_plan.target_x, .4601470588)
        self.assertAlmostEqual(far_plan.gap, .2601470588)
        self.assertEqual(near_plan.stage, "climb")
        self.assertEqual(near_plan.decision.key, "jump_climb_right")

    def test_rope_approach_targets_nearest_zone_edge(self):
        rope_x, near_range = .492600, .022500
        left = MinimapObservation(Point(.300000, .7), None, .9, (0, 0, 1, 1))
        right = MinimapObservation(Point(.700000, .7), None, .9, (0, 0, 1, 1))
        left_plan = move_towards_rope(left, rope_x, near_range)
        right_plan = move_towards_rope(right, rope_x, near_range)
        self.assertAlmostEqual(left_plan.target_x, .470100, places=6)
        self.assertEqual(left_plan.decision.key, "right")
        self.assertAlmostEqual(right_plan.target_x, .515100, places=6)
        self.assertEqual(right_plan.decision.key, "left")

    def test_final_edge_approach_is_calculated_not_full_two_seconds(self):
        observation = MinimapObservation(Point(.450000, .7), None, .9,
                                         (0, 0, 1, 1))
        plan = move_towards_rope(
            observation, .492600, .022500,
            estimated_final_speed=.205, final_move_safety_gain=.95,
            movement_hold_seconds=2.0, minimum_final_hold_seconds=.08,
        )
        self.assertEqual(plan.decision.key, "right")
        self.assertLess(plan.decision.duration, .2)

    def test_rope_hold_stays_fixed_until_final_edge_zone(self):
        observation = MinimapObservation(Point(.300000, .7), None, .9,
                                         (0, 0, 1, 1))
        plan = move_towards_rope(
            observation, .492600, .022500,
            final_calculation_distance=.04,
            estimated_final_speed=.205,
            movement_hold_seconds=2.0,
        )
        self.assertEqual(plan.decision.key, "right")
        self.assertEqual(plan.decision.duration, 2.0)
        self.assertIn("outside edge zone", plan.decision.reason)

    def test_rope_hold_reduces_only_inside_final_edge_zone(self):
        observation = MinimapObservation(Point(.450000, .7), None, .9,
                                         (0, 0, 1, 1))
        plan = move_towards_rope(
            observation, .492600, .022500,
            final_calculation_distance=.04,
            estimated_final_speed=.205,
            final_move_safety_gain=.95,
            movement_hold_seconds=2.0,
            minimum_final_hold_seconds=.08,
        )
        self.assertLess(plan.decision.duration, 2.0)
        self.assertIn("inside edge zone", plan.decision.reason)

    def test_rope_edge_movement_enforces_configured_minimum_hold(self):
        observation = MinimapObservation(Point(.465000, .7), None, .9,
                                         (0, 0, 1, 1))
        plan = move_towards_rope(
            observation, .492600, .022500,
            estimated_final_speed=.205,
            movement_hold_seconds=2.0,
            minimum_movement_hold_seconds=.50,
        )
        self.assertEqual(plan.decision.key, "right")
        self.assertEqual(plan.decision.duration, .50)

    def test_climb_direction_comes_from_character_side_of_rope(self):
        rope_x = .492647
        left_side = MinimapObservation(Point(.480000, .70), None, .9, (0, 0, 1, 1))
        right_side = MinimapObservation(Point(.505000, .70), None, .9, (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(left_side, rope_x, .032500).decision.key,
            "jump_climb_right",
        )
        self.assertEqual(
            move_towards_rope(right_side, rope_x, .032500).decision.key,
            "jump_climb_left",
        )

    def test_directional_jump_holds_up_immediately_without_gap(self):
        class Sender:
            dry_run = True
            def __init__(self):
                self.events = []
            def key_down(self, key):
                self.events.append(("down", key))
                return True
            def key_up(self, key):
                self.events.append(("up", key))
                return True
            def press(self, key, duration=0):
                self.events.append(("press", key, duration))
                return True

        sender = Sender()
        with patch("movement_worker.time.sleep") as sleep:
            from movement_worker import _directional_jump_climb
            self.assertTrue(_directional_jump_climb(sender, "right", .10, .45))
        # The only sleep is the Alt+Right chord hold. There is no post-jump
        # delay before press("up") begins.
        sleep.assert_called_once_with(.10)
        self.assertEqual(sender.events[-1], ("press", "up", .45))

    def test_persistent_climb_keeps_up_owned_until_next_layer(self):
        class Sender:
            dry_run = True
            def __init__(self): self.events = []; self.owned = set()
            def key_down(self, key):
                self.events.append(("down", key)); self.owned.add(key); return True
            def key_up(self, key):
                self.events.append(("up", key)); self.owned.discard(key); return True
            def press(self, key, duration=0):
                self.events.append(("press", key, duration)); return True

        sender, state = Sender(), ClimbState()
        start = MinimapObservation(Point(.48, .70), None, .9, (0, 0, 1, 1))
        moving = MinimapObservation(Point(.49, .66), None, .9, (0, 0, 1, 1))
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, start, state,
                                   preferred_direction="right", persistent_up=True),
                             "right-toward-rope")
            self.assertIn("up", sender.owned)
            self.assertEqual(climb(sender, moving, state,
                                   preferred_direction="right", persistent_up=True),
                             "climbing-up")
            self.assertIn("up", sender.owned)
        self.assertNotIn(("up", "up"), sender.events)

    def test_persistent_climb_uses_world_y_when_marker_stays_centered(self):
        class Sender:
            dry_run = True
            def __init__(self): self.owned = set()
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender, state = Sender(), ClimbState()
        start = MinimapObservation(
            Point(.48, .467647), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-.14, structure_confidence=.9,
        )
        scrolling = MinimapObservation(
            Point(.48, .467647), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-1.00, structure_confidence=.9,
        )
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, start, state,
                                   preferred_direction="right", persistent_up=True),
                             "right-toward-rope")
            self.assertEqual(climb(sender, scrolling, state,
                                   preferred_direction="right", persistent_up=True),
                             "climbing-up")
        self.assertEqual(state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

    def test_stalled_world_y_releases_up_and_restarts_recovery(self):
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up",
            baseline_world_y=0.0,
            up_held=True,
            last_world_y=-1.0,
        )
        stalled = MinimapObservation(
            Point(.48, .467647), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-1.02, structure_confidence=.9,
        )
        self.assertEqual(climb(sender, stalled, state, persistent_up=True),
                         "climbing-up")
        self.assertEqual(climb(sender, stalled, state, persistent_up=True),
                         "climb-stalled-retry")
        self.assertEqual(state.phase, "idle")
        self.assertNotIn("up", sender.owned)

    def test_persistent_climb_waits_for_capture_lag_before_releasing_up(self):
        class Sender:
            dry_run = True
            def __init__(self): self.owned = set()
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender, state = Sender(), ClimbState()
        centered = MinimapObservation(
            Point(.48, .467647), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-.14, structure_confidence=.9,
        )
        with patch("movement_worker.time.sleep"):
            climb(sender, centered, state,
                  preferred_direction="right", persistent_up=True)
            for _ in range(3):
                self.assertEqual(
                    climb(sender, centered, state,
                          preferred_direction="right", persistent_up=True),
                    "holding-up-awaiting-progress",
                )
                self.assertIn("up", sender.owned)

    def test_climb_releases_up_only_after_stable_next_layer_confirmation(self):
        class Sender:
            def __init__(self): self.released = []
            def key_up(self, key): self.released.append(key); return True

        positions = {
            "layer1": {
                "layer_world_y": 0.0, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
            "layer2": {
                "layer_world_y": -7.0, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions=positions, route_order=["layer1", "layer2"],
            climb_layer_confirm_frames=3,
            climb_layer_confirm_seconds=0,
        )
        worker._route_layer_index = 0
        worker._route_phase = "rope"
        worker._climb_state = ClimbState(phase="climbing-up", up_held=True)
        upper = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-7.1, structure_confidence=.9,
        )

        self.assertEqual(worker._resync_route_layer(upper), "layer1")
        self.assertEqual(worker._resync_route_layer(upper), "layer1")
        self.assertEqual(worker._route_layer_index, 0)
        self.assertEqual(sender.released, [])
        self.assertEqual(worker._resync_route_layer(upper), "layer2")
        self.assertEqual(worker._route_layer_index, 1)
        self.assertEqual(sender.released, ["up"])

    def test_fast_capture_does_not_shorten_layer_arrival_confirmation(self):
        class Sender:
            def __init__(self): self.released = []
            def key_up(self, key): self.released.append(key); return True

        positions = {
            "layer1": {
                "layer_world_y": 0.0, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
            "layer2": {
                "layer_world_y": -7.0, "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions=positions, route_order=["layer1", "layer2"],
            climb_layer_confirm_frames=3,
            climb_layer_confirm_seconds=1.0,
        )
        worker._route_layer_index = 0
        worker._climb_state = ClimbState(phase="climbing-up", up_held=True)
        upper = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-7.1, structure_confidence=.9,
        )
        y_flicker = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-3.0, structure_confidence=.9,
        )

        with patch("movement_worker.time.monotonic", side_effect=[0, 1.05]):
            for _ in range(3):
                self.assertEqual(worker._resync_route_layer(upper), "layer1")
            self.assertEqual(sender.released, [])
            self.assertEqual(worker._climb_state.phase, "arrival-compensation")
            self.assertEqual(worker._resync_route_layer(y_flicker), "layer2")
        self.assertEqual(sender.released, ["up"])

    def test_next_layer_y_controls_climb_completion(self):
        positions = {
            "layer1": {"layer_y": .698864, "y_tolerance": .02,
                       "left_most_pos": {"x": .2, "y": .698864},
                       "right_most_pos": {"x": .8, "y": .698864}},
            "layer2": {"layer_y": .565341, "y_tolerance": .02,
                       "left_most_pos": {"x": .28, "y": .565341},
                       "right_most_pos": {"x": .63, "y": .565341}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.4926,
            important_positions=positions, route_order=["layer1", "layer2"],
        )
        worker._route_layer_index = 0
        self.assertFalse(worker._next_layer_reached(
            MinimapObservation(Point(.49, .62), None, .9, (0, 0, 1, 1))))
        self.assertTrue(worker._next_layer_reached(
            MinimapObservation(Point(.49, .565341), None, .9, (0, 0, 1, 1))))

    def test_aligned_marker_keeps_last_approach_direction(self):
        rope_x = .492647
        aligned = MinimapObservation(Point(rope_x, .70), None, .9, (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(aligned, rope_x, .032500,
                              aligned_direction="right").decision.key,
            "jump_climb_right",
        )
        self.assertEqual(
            move_towards_rope(aligned, rope_x, .032500,
                              aligned_direction="left").decision.key,
            "jump_climb_left",
        )

    def test_layer_route_is_left_then_right_then_rope(self):
        positions = {
            "layer1": {
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            fixed_target_x=.5, important_positions=positions,
        )
        at_left = MinimapObservation(Point(.2, .7), None, .9, (0, 0, 1, 1))
        target, is_rope, label = worker._route_target(at_left)
        self.assertEqual((target, is_rope, label), (.2, False, "layer1.left-most"))
        self.assertTrue(worker._advance_route_endpoint(at_left, target))
        target, is_rope, label = worker._route_target(at_left)
        self.assertEqual((target, is_rope, label), (.8, False, "layer1.right-most"))
        at_right = MinimapObservation(Point(.8, .7), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._advance_route_endpoint(at_right, target))
        self.assertEqual(worker._route_target(at_right), (.5, True, "layer1.rope"))

    def test_single_layer_patrol_repeats_without_climbing(self):
        positions = {
            "layer1": {
                "layer_y": .7,
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            fixed_target_x=.5,
            important_positions=positions,
            route_order=["layer1"],
            climbing_enabled=False,
            final_layer_action="repeat_patrol",
        )
        worker._route_layer_index = 0
        worker._route_phase = "right"
        at_right = MinimapObservation(Point(.8, .7), None, .9, (0, 0, 1, 1))

        self.assertTrue(worker._advance_route_endpoint(at_right, .8))
        self.assertEqual(worker._route_phase, "left")
        self.assertEqual(
            worker._route_target(at_right),
            (.2, False, "layer1.left-most"),
        )

    def test_single_active_layer_never_drops_even_with_multilayer_final_action(self):
        positions = {
            "layer1": {
                "layer_y": .7,
                "left_most_pos": {"x": .2, "y": .7},
                "rope_pos": {"x": .5, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1"],
            climbing_enabled=True, final_layer_action="drop_to_first_layer",
        )
        worker._route_layer_index = 0
        worker._route_phase = "right"
        at_right = MinimapObservation(Point(.8, .7), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._advance_route_endpoint(at_right, .8))
        self.assertEqual(worker._route_phase, "left")
        self.assertEqual(worker._route_target(at_right)[2], "layer1.left-most")

    def test_paused_patrol_does_not_fall_back_to_rope(self):
        positions = {
            "layer1": {
                "layer_y": .7,
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1"],
            patrol_enabled=False, climbing_enabled=False,
            final_layer_action="repeat_patrol",
        )
        observation = MinimapObservation(Point(.4, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(
            worker._route_target(observation),
            (None, False, "patrol-paused"),
        )

    def test_each_layer_uses_its_own_recorded_rope(self):
        positions = {
            "layer1": {
                "layer_y": .7,
                "left_most_pos": {"x": .2, "y": .7},
                "rope_pos": {"x": .45, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            },
            "layer2": {
                "layer_y": .5,
                "left_most_pos": {"x": .3, "y": .5},
                "rope_pos": {"x": .62, "y": .5},
                "right_most_pos": {"x": .7, "y": .5},
            },
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
            climbing_enabled=True, final_layer_action="repeat_patrol",
        )
        worker._route_layer_index = 0
        worker._route_phase = "rope"
        layer1 = MinimapObservation(Point(.4, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(worker._route_target(layer1), (.45, True, "layer1.rope"))

        worker._route_layer_index = 1
        worker._route_phase = "rope"
        layer2 = MinimapObservation(Point(.6, .5), None, .9, (0, 0, 1, 1))
        # The current top layer repeats patrol rather than attempting a climb.
        self.assertEqual(worker._route_target(layer2), (.3, False, "layer2.left-most"))

    def test_map_profile_route_order_is_explicit(self):
        positions = {
            "layer1": {"left_most_pos": {"x": .1, "y": .7},
                       "right_most_pos": {"x": .9, "y": .7}},
            "layer2": {"left_most_pos": {"x": .2, "y": .5},
                       "right_most_pos": {"x": .8, "y": .5}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer2", "layer1"],
        )
        self.assertEqual(worker._route_layers, ["layer2", "layer1"])

    def test_layer_is_selected_by_explicit_y_with_tolerance(self):
        positions = {
            "layer1": {
                "layer_y": .698864, "y_tolerance": .020000,
                "left_most_pos": {"x": .2, "y": .1},
                "right_most_pos": {"x": .8, "y": .1},
            },
            "layer2": {
                "layer_y": .500000, "y_tolerance": .020000,
                "left_most_pos": {"x": .2, "y": .9},
                "right_most_pos": {"x": .8, "y": .9},
            },
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
        )
        worker._select_route_layer(Point(.5, .698000))
        self.assertEqual(worker._route_layer_index, 0)

        unknown = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
        )
        unknown._select_route_layer(Point(.5, .600000))
        self.assertIsNone(unknown._route_layer_index)

    def test_centered_marker_uses_structure_world_y_for_layer_and_fall(self):
        positions = {
            "layer1": {
                "layer_y": .5, "layer_world_y": 0.0,
                "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
            "layer2": {
                "layer_y": .5, "layer_world_y": -3.0,
                "world_y_tolerance": .75,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1", "layer2"],
        )
        upper = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-3.1, structure_confidence=.9,
        )
        worker._select_route_layer(upper)
        self.assertEqual(worker._route_layer_index, 1)

        fallen = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1),
            world_y_diamonds=.1, structure_confidence=.9,
        )
        self.assertEqual(worker._resync_route_layer(fallen), "layer1")
        self.assertEqual(worker._route_layer_index, 0)
        self.assertEqual(detect_layer_by_world_y(-3.1, positions), "layer2")

    def test_single_layer_start_uses_its_only_y_route_when_tolerance_misses(self):
        positions = {
            "layer1": {
                "layer_y": .467647, "y_tolerance": .019318,
                "left_most_pos": {"x": .413295, "y": .467647},
                "right_most_pos": {"x": .644509, "y": .467647},
            },
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(), fixed_target_x=.5,
            important_positions=positions, route_order=["layer1"],
        )
        worker._select_route_layer(Point(.542424, .497159))
        self.assertEqual(worker._route_layer_index, 0)

    def test_route_stops_after_last_calibrated_layer(self):
        positions = {"layer1": {
            "left_most_pos": {"x": .2, "y": .7},
            "right_most_pos": {"x": .8, "y": .7},
        }}
        worker = MovementWorker(queue.Queue(), object(), threading.Event(),
                                fixed_target_x=.5, important_positions=positions)
        worker._route_layer_index = 0
        worker._route_phase = "rope"
        worker._advance_after_climb()
        observation = MinimapObservation(Point(.5, .5), Point(.9, .2), .9, (0, 0, 1, 1))
        self.assertEqual(worker._route_target(observation), (None, False, "route-complete"))

    def test_detects_yellow_diamond(self):
        minimap = np.zeros((120, 200, 3), dtype=np.uint8)
        diamond(minimap, 140, 80)
        point, confidence = detect_marker(minimap)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 0.70, places=2)
        self.assertAlmostEqual(point.y, 2 / 3, places=2)
        self.assertGreater(confidence, 0.55)

    def test_unknown_map_waits(self):
        observation = MinimapObservation(Point(.5, .5), None, .9, (0, 0, 1, 1))
        self.assertIsNone(plan_movement(observation).key)

    def test_approaches_then_climbs(self):
        left = MinimapObservation(Point(.7, .7), Point(.5, .7), .9, (0, 0, 1, 1))
        aligned = MinimapObservation(Point(.5, .7), Point(.509, .7), .9, (0, 0, 1, 1))
        self.assertEqual(plan_movement(left).key, "left")
        self.assertEqual(plan_movement(left).duration, 2.0)
        self.assertEqual(plan_movement(aligned).key, "jump_climb_right")

    def test_direction_uses_configured_hold_duration(self):
        observation = MinimapObservation(Point(.2, .7), None, .9, (0, 0, 1, 1))
        decision = plan_movement(observation, fixed_target_x=.8,
                                 movement_hold_seconds=1.25)
        self.assertEqual(decision.key, "right")
        self.assertEqual(decision.duration, 1.25)

    def test_fixed_steps_then_jump_toward_rope(self):
        def decision(distance):
            player_x = .5 - distance
            observation = MinimapObservation(Point(player_x, .7), None, .9,
                                             (0, 0, 1, 1))
            return plan_movement(observation, fixed_target_x=.5)

        for distance in (.40, .30, .20, .10):
            self.assertEqual(decision(distance).duration, 2.0)
        self.assertEqual(decision(.04).key, "jump_climb_right")

    def test_near_rope_does_not_use_tiny_walk(self):
        observation = MinimapObservation(Point(.3, .7), None, .9,
                                         (0, 0, 1, 1))
        decision = plan_movement(observation, fixed_target_x=.5,
                                 estimated_minimap_speed=.2,
                                 estimated_final_speed=.2,
                                 final_calculation_distance=.25,
                                 final_move_safety_gain=1.0,
                                 horizontal_tolerance=.01)
        self.assertEqual(decision.key, "jump_climb_right")

    def test_important_endpoint_keeps_fixed_hold_when_near(self):
        observation = MinimapObservation(Point(.19, .7), None, .9,
                                         (0, 0, 1, 1))
        decision = plan_movement(
            observation,
            fixed_target_x=.20,
            horizontal_tolerance=.001,
            final_calculation_distance=.04,
            movement_hold_seconds=2.0,
            jump_when_near=False,
        )
        self.assertEqual(decision.key, "right")
        self.assertEqual(decision.duration, 2.0)

    def test_passing_important_endpoint_advances_route(self):
        positions = {"layer1": {
            "left_most_pos": {"x": .2, "y": .7},
            "right_most_pos": {"x": .8, "y": .7},
        }}
        worker = MovementWorker(queue.Queue(), object(), threading.Event(),
                                fixed_target_x=.5, important_positions=positions)
        worker._route_layer_index = 0
        worker._route_phase = "left"
        crossed_left = MinimapObservation(Point(.17, .7), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._advance_route_endpoint(crossed_left, .2))
        self.assertEqual(worker._route_phase, "right")
        crossed_right = MinimapObservation(Point(.83, .7), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._advance_route_endpoint(crossed_right, .8))
        self.assertEqual(worker._route_phase, "rope")

    def test_saved_rope_position_overrides_connector_detection(self):
        observation = MinimapObservation(Point(.70, .70), None, .90, (0, 0, 1, 1))
        self.assertEqual(plan_movement(observation, fixed_target_x=.25).key, "left")
        aligned = MinimapObservation(Point(.251, .70), None, .90, (0, 0, 1, 1))
        self.assertEqual(plan_movement(aligned, fixed_target_x=.25).key, "jump_climb_left")

    def test_capture_frame_contract(self):
        # This intentionally uses the integration's immutable frame wrapper.
        image = np.zeros((400, 600, 3), dtype=np.uint8)
        # Marker lies inside the real normalized minimap-interior crop.
        diamond(image, 60, 60)
        frame = object.__new__(CapturedFrame)
        object.__setattr__(frame, "sequence", 1)
        object.__setattr__(frame, "captured_at", None)
        object.__setattr__(frame, "captured_monotonic", 1.0)
        object.__setattr__(frame, "image", Image.fromarray(image))
        object.__setattr__(frame, "window_rect", (0, 0, 600, 400))
        self.assertIsNotNone(analyze_minimap(frame).player)

    def test_climb_sends_jump_then_up(self):
        class Sender:
            dry_run = True
            def __init__(self):
                self.keys = []
            def press(self, key, duration=0):
                self.keys.append((key, duration))
                return True
            def key_down(self, key):
                self.keys.append((f"{key}-down", 0))
                return True
            def key_up(self, key):
                self.keys.append((f"{key}-up", 0))
                return True
            def key_down(self, key):
                self.keys.append((f"{key}-down", 0))
                return True
            def key_up(self, key):
                self.keys.append((f"{key}-up", 0))
                return True

        sender = Sender()
        with patch("movement_worker.time.sleep"):
            success = _send_tap(sender, MovementDecision("climb", "test", .45))
        self.assertTrue(success)
        self.assertEqual(sender.keys, [("alt", .025), ("up", .45)])

    def test_failed_climb_reports_failure_for_retry(self):
        class Sender:
            dry_run = True
            def press(self, key, duration=0):
                return key != "alt"

        with patch("movement_worker.time.sleep"):
            self.assertFalse(_send_tap(Sender(), MovementDecision("climb", "test", .45)))

    def test_climb_starts_directional_then_tries_opposite_and_checks_y(self):
        class Sender:
            dry_run = True
            def __init__(self):
                self.keys = []
            def press(self, key, duration=0):
                self.keys.append((key, duration))
                return True
            def key_down(self, key):
                self.keys.append((f"{key}-down", 0))
                return True
            def key_up(self, key):
                self.keys.append((f"{key}-up", 0))
                return True

        def observation(y):
            return MinimapObservation(Point(.5, y), None, .9, (0, 0, 1, 1))

        sender, state = Sender(), ClimbState()
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, observation(.70), state,
                                   preferred_direction="right"), "right-toward-rope")
            self.assertEqual(climb(sender, observation(.70), state),
                             "left-retry-toward-rope")
            self.assertEqual(climb(sender, observation(.66), state), "succeeded")
        self.assertEqual([key for key, _ in sender.keys], [
            "right-down", "alt-down", "alt-up", "right-up", "up",
            "left-down", "alt-down", "alt-up", "left-up", "up",
        ])

    def test_climb_does_not_treat_falling_as_success(self):
        class Sender:
            dry_run = True
            def press(self, key, duration=0):
                return True
            def key_down(self, key):
                return True
            def key_up(self, key):
                return True
        state = ClimbState()
        with patch("movement_worker.time.sleep"):
            climb(Sender(), MinimapObservation(Point(.5, .5), None, .9, (0, 0, 1, 1)),
                  state, preferred_direction="left")
            result = climb(Sender(), MinimapObservation(Point(.5, .6), None, .9, (0, 0, 1, 1)), state)
        self.assertEqual(result, "right-retry-toward-rope")

    def test_retry_does_not_reverse_when_character_stays_left_of_rope(self):
        class Sender:
            dry_run = True
            def __init__(self):
                self.events = []
            def press(self, key, duration=0):
                self.events.append(("press", key))
                return True
            def key_down(self, key):
                self.events.append(("down", key))
                return True
            def key_up(self, key):
                self.events.append(("up", key))
                return True

        # Both fresh observations say character X is still left of Rope X.
        observation = MinimapObservation(Point(.480000, .70), None, .9,
                                         (0, 0, 1, 1))
        sender, state = Sender(), ClimbState()
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, observation, state,
                                   preferred_direction="right"),
                             "right-toward-rope")
            self.assertEqual(climb(sender, observation, state,
                                   preferred_direction="right"),
                             "right-retry-toward-rope")
        direction_downs = [key for event, key in sender.events
                           if event == "down" and key in ("left", "right")]
        self.assertEqual(direction_downs, ["right", "right"])

    def test_failed_directional_round_shifts_right_point_zero_one_seconds(self):
        class Sender:
            dry_run = True
            def __init__(self):
                self.events = []
            def press(self, key, duration=0):
                self.events.append((key, duration))
                return True
            def key_down(self, key):
                self.events.append((f"{key}-down", 0))
                return True
            def key_up(self, key):
                self.events.append((f"{key}-up", 0))
                return True

        observation = MinimapObservation(Point(.48, .70), None, .9, (0, 0, 1, 1))
        sender, state = Sender(), ClimbState()
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, observation, state,
                                   preferred_direction="right"), "right-toward-rope")
            self.assertEqual(climb(sender, observation, state),
                             "left-retry-toward-rope")
            result = climb(sender, observation, state,
                           failed_cycle_right_seconds=.01)
        self.assertEqual(result, "failed-cycle-shifted-right")
        self.assertEqual(sender.events[-1], ("right", .01))
        self.assertEqual(state.phase, "idle")
        self.assertTrue(state.failed_shift_used)

        # Another complete failed round during the same near-rope approach
        # must not accumulate another right correction.
        right_count = sender.events.count(("right", .01))
        with patch("movement_worker.time.sleep"):
            climb(sender, observation, state, preferred_direction="right")
            climb(sender, observation, state)
            second_result = climb(sender, observation, state,
                                  failed_cycle_right_seconds=.01)
        self.assertEqual(second_result, "failed-cycle-no-more-shift")
        self.assertEqual(sender.events.count(("right", .01)), right_count)

    def test_half_second_rope_distance_boundary(self):
        speed = .20
        half_second_distance = speed * .5
        outside = MinimapObservation(Point(.5 - half_second_distance - .001, .7),
                                     None, .9, (0, 0, 1, 1))
        inside = MinimapObservation(Point(.5 - half_second_distance, .7),
                                    None, .9, (0, 0, 1, 1))
        self.assertEqual(plan_movement(
            outside, fixed_target_x=.5, estimated_final_speed=speed,
            final_calculation_distance=half_second_distance).key, "right")
        self.assertEqual(plan_movement(
            inside, fixed_target_x=.5, estimated_final_speed=speed,
            final_calculation_distance=half_second_distance).key, "jump_climb_right")

    def test_explicit_tight_rope_range_overrides_old_tolerance(self):
        target = .4926470588
        outside = MinimapObservation(Point(target - .003, .7), None, .9,
                                     (0, 0, 1, 1))
        inside = MinimapObservation(Point(target - .0025, .7), None, .9,
                                    (0, 0, 1, 1))
        self.assertEqual(plan_movement(
            outside, fixed_target_x=target, horizontal_tolerance=.0025,
            final_calculation_distance=.0025).key, "right")
        self.assertEqual(plan_movement(
            inside, fixed_target_x=target, horizontal_tolerance=.0025,
            final_calculation_distance=.0025).key, "jump_climb_right")


if __name__ == "__main__":
    unittest.main()
