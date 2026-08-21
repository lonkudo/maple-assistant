import queue
import threading
import time
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
    PositionMovementPlan,
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
    def test_run_climb_step_releases_false_attach_on_noop_frame(self):
        # The attached no-op decision (key=None) never entered the send
        # block, so climb() (and the fell-back/stall release) never ran.
        # _run_climb_step must be driven on no-op frames too.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        worker = MovementWorker(queue.Queue(), sender, threading.Event(),
                                important_positions={})
        worker._climb_state = ClimbState(
            phase="climbing-up", baseline_y=.66, up_held=True,
            recent_y=[.66, .64, .645],
        )
        obs = MinimapObservation(Point(.48, .655), None, .9, (0, 0, 1, 1))
        result = worker._run_climb_step(obs, None, None)
        self.assertEqual(result, "climb-stalled-retry")
        self.assertEqual(worker._climb_state.phase, "idle")
        self.assertNotIn("up", sender.owned)

    def test_walk_hold_releases_early_when_attack_takes_over(self):
        import tempfile
        import time
        from pathlib import Path
        from combat_coordination import AttackStateFile

        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def is_target_focused(self): return True

        tmp = Path(tempfile.mkdtemp()) / "attack_state.json"
        attack = AttackStateFile(str(tmp))
        attack.write(False)
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions={}, attack_state_path=str(tmp),
        )
        # Full hold without attack: key down, held ~1s, key up.  Z pickup goes
        # down together with the direction and comes up with it.
        started = time.monotonic()
        self.assertTrue(worker._send_walk_hold(
            MovementDecision("left", "walk", 1.0)))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(
            sender.events,
            [("down", "left"), ("down", "z"), ("up", "left"), ("up", "z")],
        )
        # Attack takes over mid-hold: the movement key must release early
        # (~20ms) so the attack can turn the character and hit a monster
        # behind it.
        sender.events = []
        attack.write(False)

        def activate_attack():
            time.sleep(0.1)
            attack.write(True, (100.0, 100.0))

        starter = threading.Thread(target=activate_attack)
        starter.start()
        started = time.monotonic()
        self.assertTrue(worker._send_walk_hold(
            MovementDecision("left", "walk", 2.0)))
        elapsed = time.monotonic() - started
        starter.join()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(
            sender.events,
            [("down", "left"), ("down", "z"), ("up", "left"), ("up", "z")],
        )
        attack.write(False)

    def test_walk_hold_pushes_through_when_attack_window_exceeded(self):
        import tempfile
        import time
        from pathlib import Path
        from combat_coordination import AttackStateFile

        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def is_target_focused(self): return True

        tmp = Path(tempfile.mkdtemp()) / "attack_state.json"
        attack = AttackStateFile(str(tmp))
        attack.write(True, (100.0, 100.0))  # target active
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions={}, attack_state_path=str(tmp),
            attack_block_max_seconds=0.3,
        )
        # The attack has already been active past its window: the patrol
        # pushes through and the walk key is NOT released early.
        worker._attack_active_since = time.monotonic() - 1.0
        started = time.monotonic()
        self.assertTrue(worker._send_walk_hold(
            MovementDecision("left", "walk", 1.0)))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(
            sender.events,
            [("down", "left"), ("down", "z"), ("up", "left"), ("up", "z")],
        )
        attack.write(False)

    def test_walk_hold_z_pickup_is_simultaneous_and_gated(self):
        # Z is pressed together with the direction key and released with it;
        # the pickup-active event is set for the whole hold (so climb/jump
        # logic waits) and cleared afterwards.
        import tempfile
        from pathlib import Path
        from combat_coordination import AttackStateFile

        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def is_target_focused(self): return True

        tmp = Path(tempfile.mkdtemp()) / "attack_state.json"
        attack = AttackStateFile(str(tmp))
        attack.write(False)
        pickup_active = threading.Event()
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions={}, attack_state_path=str(tmp),
            pickup_active_event=pickup_active,
        )

        observed = []

        def watch_event():
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if pickup_active.is_set():
                    observed.append(True)
                    return
                time.sleep(0.005)

        watcher = threading.Thread(target=watch_event)
        watcher.start()
        self.assertTrue(worker._send_walk_hold(
            MovementDecision("right", "walk", 0.5)))
        watcher.join()
        self.assertTrue(observed,
                        "pickup-active event was never set during the hold")
        self.assertFalse(pickup_active.is_set())
        self.assertGreaterEqual(worker._pickup_count, 1)
        # Event order: direction down first, Z down next; direction up first,
        # Z up next - simultaneous pairs.
        self.assertEqual(
            sender.events,
            [("down", "right"), ("down", "z"), ("up", "right"), ("up", "z")],
        )

    def test_rope_approach_stall_detects_on_rope_and_starts_climb(self):
        # 角色卡在绳中段：走向绳子的方向键让 X 不再前进（角色在绳上），
        # 停滞检测触发后必须改为爬绳（Up），不再按方向键+Z。
        class Sender:
            dry_run = True
            def __init__(self): self.owned = set()
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions={},
        )
        # 角色 X 停在绳上（0.43，绳 0.449，gap 0.019）：首帧初始化，
        # 之后连续无进展 5 帧后判定停滞。
        for _ in range(5):
            self.assertFalse(worker._rope_approach_stalled(
                0.430556, 0.449074, "layer2.rope"))
        self.assertTrue(worker._rope_approach_stalled(
            0.430556, 0.449074, "layer2.rope"))
        # X 前进了 → 计数器重置。
        self.assertFalse(worker._rope_approach_stalled(
            0.440000, 0.449074, "layer2.rope"))
        # 不在绳的 X 附近（未对齐）→ 不判定停滞。
        for _ in range(6):
            self.assertFalse(worker._rope_approach_stalled(
                0.200000, 0.449074, "layer2.rope"))

        # 停滞恢复：角色在绳上 → 启动爬绳状态（Up 按住，不再发方向键）。
        worker._climb_state.phase = "idle"
        stuck_obs = MinimapObservation(
            Point(0.430556, 0.397196), None, .9, (0, 0, 1, 1)
        )
        worker._start_rope_stuck_climb(stuck_obs)
        self.assertEqual(worker._climb_state.phase, "climbing-up")
        self.assertTrue(worker._climb_state.up_held)
        self.assertIn("up", sender.owned)
        # 重复调用不重复处理（已在爬）。
        worker._start_rope_stuck_climb(stuck_obs)
        self.assertEqual(worker._rope_stuck_recoveries, 1)

    def test_rope_approach_recovery_jumps_when_blocked_at_platform_edge(self):
        from unittest import mock

        class Sender:
            dry_run = True
            def __init__(self): self.owned = set()
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions={"layer1": {
                "layer_y": .67, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .67},
                "right_most_pos": {"x": .8, "y": .67},
            }},
            route_order=["layer1"],
        )
        worker._route_layers = ["layer1"]
        worker._climb_state.phase = "idle"
        # 平台边缘：角色在 layer1 平台上（Y 命中 band），X 停在绳旁无法前进
        # → 朝绳方向起跳（交给爬绳状态机）。
        edge = MinimapObservation(Point(.234, .67), None, .9, (0, 0, 1, 1))
        with mock.patch.object(worker, "_run_climb_step") as climb:
            worker._recover_rope_approach(edge, 0.215)
            climb.assert_called_once()
            self.assertEqual(climb.call_args[0][1], 0.215)  # rope_x
            self.assertEqual(climb.call_args[0][2], "left")  # 朝绳方向
        # 在绳上（Y 不在任何平台 band）→ 爬绳（Up 按住），不起跳。
        worker._climb_state.phase = "idle"
        on_rope_obs = MinimapObservation(Point(.43, .397), None, .9, (0, 0, 1, 1))
        worker._recover_rope_approach(on_rope_obs, 0.449)
        self.assertEqual(worker._climb_state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

    def test_self_rescue_triggers_after_20_stuck_frames_in_window(self):
        from unittest import mock

        # 5 分钟窗口内角色连续 20 帧位置不变（卡住）→ 触发自救；位置有
        # 变化则计数重置；攻击期间不计。
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={"layer1": {
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }},
            route_order=["layer1"],
            rescue_check_interval_seconds=300.0,
            rescue_stuck_frames=20,
        )
        worker.patrol_enabled = True
        worker._rescue_last_check = 0.0
        worker._rescue_last_pos = None
        stuck = MinimapObservation(Point(.5, .5), None, .9, (0, 0, 1, 1))
        with mock.patch.object(worker, "_trigger_rescue") as rescue:
            # 前 19 帧位置不变（首帧为初始化）：窗口未结束，不触发。
            for _ in range(19):
                worker._rescue_stuck_check(stuck, 100.0)
            self.assertEqual(worker._rescue_max_stuck, 18)
            rescue.assert_not_called()
            # 连续无进展累计达到 20 帧 → 窗口结束时触发。
            worker._rescue_stuck_check(stuck, 100.0)
            worker._rescue_stuck_check(stuck, 100.0)
            self.assertEqual(worker._rescue_max_stuck, 20)
            worker._rescue_stuck_check(stuck, 400.0)  # 下一个 5 分钟窗口
            rescue.assert_called_once()
            # 窗口重置后：位置变化 → 计数清零。
            moved = MinimapObservation(Point(.6, .5), None, .9, (0, 0, 1, 1))
            worker._rescue_stuck_check(moved, 401.0)
            self.assertEqual(worker._rescue_stuck_frames, 0)

    def test_reset_route_loop_clears_dropping_flag(self):
        # After the drop phase reaches layer1 a new loop starts - the
        # dropping flag must clear, otherwise the patrol stays busy forever
        # and the YOLO attack is blocked ("attack blocked: patrol
        # climbing/dropping" - the character never attacks).
        dropping = threading.Event()
        dropping.set()
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={"layer1": {
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }},
            route_order=["layer1"], first_layer="layer1",
            dropping_active_event=dropping,
        )
        self.assertTrue(dropping.is_set())
        worker._reset_route_loop()
        self.assertFalse(dropping.is_set())
        self.assertEqual(worker._route_phase, "left")

    def test_climb_arrival_detected_by_marker_y_when_world_y_sticks(self):
        # The user's map: the world-Y tracker re-anchors to layer1 (11.32)
        # during the climb, but the minimap marker Y reaches layer2's band -
        # the arrival must still fire (marker signal wins over the stale
        # lower layer).
        class Sender:
            def __init__(self): self.released = []
            def key_up(self, key): self.released.append(key); return True

        positions = {
            "layer1": {
                "layer_world_y": 11.32, "world_y_tolerance": .75,
                "layer_y": .775391, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
            "layer2": {
                "layer_world_y": -17.15, "world_y_tolerance": .75,
                "layer_y": .611328, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions=positions, route_order=["layer1", "layer2"],
            climb_layer_confirm_frames=2,
            climb_layer_confirm_seconds=0,
        )
        worker._route_layer_index = 0
        worker._route_phase = "rope"
        worker._climb_state = ClimbState(phase="climbing-up", up_held=True)
        # Marker Y in layer2's band; world Y stuck at layer1's value.
        at_layer2 = MinimapObservation(
            Point(.09, .607422), None, .9, (0, 0, 1, 1),
            world_y_diamonds=11.32, structure_confidence=.9,
        )
        self.assertEqual(worker._resync_route_layer(at_layer2), "layer1")
        self.assertEqual(worker._resync_route_layer(at_layer2), "layer2")
        self.assertEqual(worker._route_layer_index, 1)

    def test_fell_back_suppressed_at_next_layer_arrival(self):
        # The character reached the top and the marker settled at the NEXT
        # layer's Y: not a failed grab - Up stays held so the arrival can
        # complete the climb.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up", baseline_y=.587891, up_held=True,
            recent_y=[.60, .587891],
        )
        settled = MinimapObservation(
            Point(.09, .607422), None, .9, (0, 0, 1, 1)
        )
        self.assertEqual(climb(sender, settled, state, persistent_up=True,
                               arrival_y=.611328, arrival_tolerance=.02),
                         "climbing-up")
        self.assertEqual(state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

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

        # Outside the inner gap but inside the honey zone: a tiny random step
        # toward the rope center - never a big walk, never a jump.
        self.assertEqual(band_plan.decision.key, "right")
        self.assertAlmostEqual(band_plan.target_x, rope_x, places=6)
        self.assertLess(band_plan.decision.duration, .2)
        # Inside the inner gap the climb attempt happens immediately.
        self.assertEqual(inner_plan.decision.key, "jump_climb_right")
        # Beyond the honey zone the character walks toward the honey edge.
        self.assertEqual(far_plan.decision.key, "right")

    def test_honey_zone_uses_tiny_random_steps_and_outside_big_walking(self):
        rope_x, near_range, inner_range = .492600, .022500, .010000
        # Between the inner jump gate and the honey-zone edge: tiny random
        # step within the configured bounds - never a big walk, never a jump.
        honey = MinimapObservation(Point(.480000, .7), None, .9, (0, 0, 1, 1))
        plan = move_towards_rope(
            honey, rope_x, near_range, inner_range=inner_range,
            tiny_step_min_seconds=.05, tiny_step_max_seconds=.15,
        )
        self.assertEqual(plan.decision.key, "right")
        self.assertGreaterEqual(plan.decision.duration, .05)
        self.assertLessEqual(plan.decision.duration, .15)
        self.assertIn("honey zone", plan.decision.reason)
        # Outside the honey zone: big walking toward the honey edge.
        far = MinimapObservation(Point(.300000, .7), None, .9, (0, 0, 1, 1))
        plan = move_towards_rope(
            far, rope_x, near_range, inner_range=inner_range,
            movement_hold_seconds=2.0, estimated_final_speed=.205,
        )
        self.assertEqual(plan.decision.key, "right")
        self.assertEqual(plan.decision.duration, 2.0)
        self.assertAlmostEqual(plan.target_x, rope_x - near_range, places=6)

    def test_rope_three_gap_zones_with_new_thresholds(self):
        # Right on the rope (|gap| <= 0.008): jump straight up.
        rope_x, near, inner = .492600, .025000, .018000
        under = MinimapObservation(Point(rope_x - .007, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(under, rope_x, near, inner_range=inner).decision.key,
            "jump_climb_up",
        )
        # Inner band (0.008 < |gap| <= 0.018): jump toward the rope side.
        left_of_rope = MinimapObservation(
            Point(rope_x - .010, .7), None, .9, (0, 0, 1, 1))
        right_of_rope = MinimapObservation(
            Point(rope_x + .012, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(left_of_rope, rope_x, near,
                              inner_range=inner).decision.key,
            "jump_climb_right",
        )
        self.assertEqual(
            move_towards_rope(right_of_rope, rope_x, near,
                              inner_range=inner).decision.key,
            "jump_climb_left",
        )
        # Honey zone (0.018 < |gap| <= 0.025): tiny random step to adjust.
        honey = MinimapObservation(
            Point(rope_x - .020, .7), None, .9, (0, 0, 1, 1))
        plan = move_towards_rope(honey, rope_x, near, inner_range=inner,
                                 tiny_step_min_seconds=.05,
                                 tiny_step_max_seconds=.15)
        self.assertEqual(plan.decision.key, "right")
        self.assertLess(plan.decision.duration, .2)
        # Beyond the honey zone: big walking.
        far = MinimapObservation(Point(rope_x - .100, .7), None, .9, (0, 0, 1, 1))
        plan = move_towards_rope(far, rope_x, near, inner_range=inner,
                                 movement_hold_seconds=2.0,
                                 estimated_final_speed=.205)
        self.assertEqual(plan.decision.key, "right")
        self.assertEqual(plan.decision.duration, 2.0)

    def test_horizontal_correction_cannot_cancel_attached_climb(self):
        state = ClimbState(phase="climbing-up", up_held=True)
        proposed = MovementDecision("right", "tiny rope-edge correction", .30)

        protected = preserve_persistent_climb(state, proposed)

        self.assertIsNone(protected.key)
        self.assertIn("Up remains held", protected.reason)

    def test_walk_deferred_while_up_held_during_grab(self):
        # Mid-grab (Up held, not attached yet): a walk decision must not
        # reach the sender and release Up - that made the character stop at
        # the middle of climbing.
        state = ClimbState(phase="check-primary-up", up_held=True)
        walk = MovementDecision("left", "minimap walk plan", 2.0)

        protected = preserve_persistent_climb(state, walk)

        self.assertIsNone(protected.key)
        self.assertIn("Up remains held", protected.reason)
        # Climb decisions still pass through so the state machine advances.
        jump = MovementDecision("jump_climb_up", "yolo jump", 0.08)
        self.assertEqual(preserve_persistent_climb(state, jump), jump)
        # Once Up is released the walk is allowed again.
        state.up_held = False
        self.assertEqual(preserve_persistent_climb(state, walk), walk)

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
        worker._route_phase = "drop"
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
        worker._route_phase = "drop"
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

    def test_fixed_mode_skips_yolo_rope_logic(self):
        # Fixed Attack mode runs without the YOLO subprocess: the screen gap
        # must never drive the rope jump, even when a fresh rope state file
        # exists - the minimap logic takes over entirely.
        import tempfile
        from pathlib import Path
        from combat_coordination import RopeStateFile

        tmp = Path(tempfile.mkdtemp()) / "rope_state.json"
        state = RopeStateFile(str(tmp))
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, rope_state_path=str(tmp),
            rope_jump_px=140.0, yolo_detection_active=False,
        )
        # Fresh state with a tight gap would normally jump via the screen
        # logic - in fixed mode it is ignored (minimap plan owns the jump).
        state.write(True, rope_x=1280.0, char_x=1280.0)
        self.assertFalse(worker._yolo_detection_active)
        self.assertIsNone(worker._yolo_rope_action())
        # Re-enabling YOLO detection restores the screen logic live.
        worker.set_yolo_detection_active(True)
        self.assertTrue(worker._yolo_detection_active)
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_up")
        worker.set_yolo_detection_active(False)
        self.assertFalse(worker._yolo_detection_active)
        self.assertIsNone(worker._yolo_rope_action())

    def _red_diamond_frame(self):
        """CapturedFrame whose whole-image minimap crop has a red diamond."""

        image = np.zeros((160, 200, 3), dtype=np.uint8)
        cy, cx, radius = 80, 100, 4
        for y in range(-radius, radius + 1):
            for x in range(-radius, radius + 1):
                if abs(x) + abs(y) <= radius:
                    image[cy + y, cx + x] = (227, 0, 0)
        frame = object.__new__(CapturedFrame)
        object.__setattr__(frame, "image", Image.fromarray(image))
        return frame

    def test_other_player_check_triggers_every_scan_when_players_present(self):
        from unittest import mock

        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, other_player_check_enabled=True,
        )
        frame = self._red_diamond_frame()
        region = (0.0, 0.0, 1.0, 1.0)
        with mock.patch.object(worker, "_trigger_other_player_switch") as trig:
            # 无冷却：连续两帧都有人 → 都触发换线。
            worker._maybe_check_other_players(0.0, frame, region)
            trig.assert_called_once()
            self.assertEqual(trig.call_args[0][0], 1)  # one red diamond
            worker._maybe_check_other_players(0.02, frame, region)
            self.assertEqual(trig.call_count, 2)
            worker._maybe_check_other_players(0.04, frame, region)
            self.assertEqual(trig.call_count, 3)

    def test_other_player_check_skips_when_disabled_or_clean(self):
        from unittest import mock

        # Disabled: never scans, never triggers.
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={},
        )
        frame = self._red_diamond_frame()
        region = (0.0, 0.0, 1.0, 1.0)
        with mock.patch.object(worker, "_other_players_on_minimap") as scan, \
                mock.patch.object(worker, "_trigger_other_player_switch") as trig:
            worker._maybe_check_other_players(0.0, frame, region)
            scan.assert_not_called()
            trig.assert_not_called()

        # Enabled and the interval elapsed, but no red diamond: no trigger.
        worker.set_other_player_check(True)
        clean = np.zeros((160, 200, 3), dtype=np.uint8)
        frame_clean = object.__new__(CapturedFrame)
        object.__setattr__(frame_clean, "image", Image.fromarray(clean))
        with mock.patch.object(worker, "_trigger_other_player_switch") as trig:
            worker._maybe_check_other_players(0.0, frame_clean, region)
            worker._maybe_check_other_players(61.0, frame_clean, region)
            trig.assert_not_called()

    def test_other_player_switch_flow_drugs_then_switches_then_resumes(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        class FakeController:
            def __init__(self):
                self.enabled = True

            def set_enabled(self, value):
                self.enabled = bool(value)

        class FakeSender:
            _SCAN = {"delete": (0x53, True), "esc": (0x01, False)}

            def __init__(self):
                self.pressed = []

            def press(self, key, duration=0.025):
                self.pressed.append(key)
                return True

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                self._target()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status_state.json"
            state_path.write_text(
                json.dumps({"hp_ratio": 0.5}), encoding="utf-8"
            )
            drug_path = Path(directory) / "drug_settings.json"
            drug_path.write_text(
                json.dumps({"hp_key": "delete"}), encoding="utf-8"
            )
            controller = FakeController()
            sender = FakeSender()
            worker = MovementWorker(
                queue.Queue(), sender, threading.Event(),
                important_positions={}, patrol_controller=controller,
                other_player_check_enabled=True,
                status_state_path=str(state_path),
                drug_settings_path=str(drug_path),
            )
            with mock.patch("movement_worker.threading.Thread", FakeThread), \
                    mock.patch("channel_switch.random.randint",
                               side_effect=[2, 1]), \
                    mock.patch("channel_switch.time.sleep"), \
                    mock.patch("movement_worker.time.sleep"):
                worker._trigger_other_player_switch(1)

            # Patrol was disabled during the switch and re-enabled after.
            self.assertTrue(controller.enabled)
            # HP 50% < 70%: progressive HP drug first (3 taps), then switch.
            self.assertEqual(sender.pressed[:3],
                             ["delete", "delete", "delete"])
            self.assertEqual(sender.pressed[3:], [
                "esc", "enter", "left", "left", "down", "enter", "esc",
            ])
            self.assertFalse(worker._player_switch_active)

    def test_switch_restarts_patrol_from_first_layer(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        class FakeSender:
            _SCAN = {"delete": (0x53, True), "esc": (0x01, False)}

            def __init__(self):
                self.pressed = []

            def press(self, key, duration=0.025):
                self.pressed.append(key)
                return True

            def key_down(self, key):
                return True

            def key_up(self, key):
                return True

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                self._target()

        class FakeTracker:
            def __init__(self):
                self.anchors = []

            def reanchor_world_y(self, world_y):
                self.anchors.append(world_y)

        positions = {
            "layer1": {
                "layer_world_y": 11.32, "layer_y": .7, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            },
            "layer2": {
                "layer_world_y": -17.15, "layer_y": .5, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        tracker = FakeTracker()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status_state.json"
            state_path.write_text(
                json.dumps({"hp_ratio": 0.9}), encoding="utf-8"
            )
            drug_path = Path(directory) / "drug_settings.json"
            drug_path.write_text(
                json.dumps({"hp_key": "delete"}), encoding="utf-8"
            )
            sender = FakeSender()
            worker = MovementWorker(
                queue.Queue(), sender, threading.Event(),
                important_positions=positions, route_order=["layer1", "layer2"],
                first_layer="layer1", structure_tracker=tracker,
                status_state_path=str(state_path),
                drug_settings_path=str(drug_path),
            )
            # Pretend the patrol was mid-layer2 before the switch.
            worker._route_layer_index = 1
            worker._route_phase = "right"
            # The new channel spawned the character ON layer1 already: the
            # drop ends on its first check.
            worker.last_observation = MinimapObservation(
                Point(.5, .7), None, .9, (0, 0, 1, 1)
            )
            with mock.patch("movement_worker.threading.Thread", FakeThread), \
                    mock.patch("channel_switch.random.randint",
                               side_effect=[1, 1]), \
                    mock.patch("channel_switch.time.sleep"), \
                    mock.patch("movement_worker.time.sleep"):
                worker._trigger_other_player_switch(1)

            # Post-switch: route reset to layer1/left and world Y re-anchored
            # to layer1 - the character respawned at the map entry.
            self.assertEqual(worker._route_layer_index, 0)
            self.assertEqual(worker._route_phase, "left")
            self.assertEqual(tracker.anchors, [11.32])

    def test_switch_skips_drug_when_hp_healthy(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        class FakeSender:
            _SCAN = {"delete": (0x53, True), "esc": (0x01, False)}

            def __init__(self):
                self.pressed = []

            def press(self, key, duration=0.025):
                self.pressed.append(key)
                return True

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                self._target()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status_state.json"
            state_path.write_text(
                json.dumps({"hp_ratio": 0.9}), encoding="utf-8"
            )
            drug_path = Path(directory) / "drug_settings.json"
            drug_path.write_text(
                json.dumps({"hp_key": "delete"}), encoding="utf-8"
            )
            sender = FakeSender()
            worker = MovementWorker(
                queue.Queue(), sender, threading.Event(),
                important_positions={},
                status_state_path=str(state_path),
                drug_settings_path=str(drug_path),
            )
            with mock.patch("movement_worker.threading.Thread", FakeThread), \
                    mock.patch("channel_switch.random.randint",
                               side_effect=[1, 1]), \
                    mock.patch("channel_switch.time.sleep"), \
                    mock.patch("movement_worker.time.sleep"):
                worker._trigger_other_player_switch(1)

            # HP 90% >= 70%: no drug - the switch sequence starts immediately.
            self.assertNotIn("delete", sender.pressed)
            self.assertEqual(sender.pressed[0], "esc")

    def test_switch_rechecks_and_switches_again_while_players_present(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        class FakeSender:
            _SCAN = {"delete": (0x53, True), "esc": (0x01, False)}

            def __init__(self):
                self.pressed = []

            def press(self, key, duration=0.025):
                self.pressed.append(key)
                return True

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                self._target()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status_state.json"
            state_path.write_text(
                json.dumps({"hp_ratio": 0.9}), encoding="utf-8"
            )
            drug_path = Path(directory) / "drug_settings.json"
            drug_path.write_text(
                json.dumps({"hp_key": "delete"}), encoding="utf-8"
            )
            sender = FakeSender()
            worker = MovementWorker(
                queue.Queue(), sender, threading.Event(),
                important_positions={},
                other_player_switch_max_attempts=2,
                status_state_path=str(state_path),
                drug_settings_path=str(drug_path),
            )
            with mock.patch("movement_worker.threading.Thread", FakeThread), \
                    mock.patch("channel_switch.random.randint",
                               side_effect=[1, 1, 1, 1]), \
                    mock.patch("channel_switch.time.sleep"), \
                    mock.patch("movement_worker.time.sleep"), \
                    mock.patch.object(worker, "_other_players_on_latest_frame",
                                      return_value=2):
                worker._trigger_other_player_switch(1)

            # New channel still busy: switched twice (max attempts), then gave
            # up.  Each switch has one left press (randint side effects).
            self.assertEqual(sender.pressed.count("left"), 2)
            self.assertFalse(worker._player_switch_active)

    def test_trigger_switch_guards_against_double_fire(self):
        from unittest import mock

        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={},
        )
        worker._player_switch_active = True
        with mock.patch.object(worker, "_run_other_player_switch") as run:
            worker._trigger_other_player_switch(1)
            run.assert_not_called()
        worker._player_switch_active = False
        with mock.patch.object(worker, "_run_other_player_switch") as run:
            worker._trigger_other_player_switch(1)
            run.assert_called_once()

    def test_switch_drop_ends_when_marker_reaches_first_layer(self):
        from unittest import mock

        class FakeSender:
            def __init__(self):
                self.events = []

            def key_down(self, key):
                self.events.append(("down", key))
                return True

            def key_up(self, key):
                self.events.append(("up", key))
                return True

        positions = {
            "layer1": {
                "layer_y": .7, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            },
            "layer2": {
                "layer_y": .5, "y_tolerance": .02,
                "left_most_pos": {"x": .2, "y": .5},
                "right_most_pos": {"x": .8, "y": .5},
            },
        }
        sender = FakeSender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(),
            important_positions=positions, route_order=["layer1", "layer2"],
            first_layer="layer1",
        )
        # The marker is visible; first check: still on layer2 -> drop;
        # second check: on layer1 -> stop.
        worker.last_observation = MinimapObservation(
            Point(.5, .5), None, .9, (0, 0, 1, 1)
        )
        with mock.patch.object(worker, "_on_first_layer",
                               side_effect=[False, True]), \
                mock.patch("movement_worker.time.sleep"):
            worker._drop_to_first_layer()
        # Exactly one Alt+Down chord, then stop.
        self.assertEqual(sender.events.count(("down", "down")), 1)
        self.assertEqual(sender.events.count(("down", "alt")), 1)

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
        # Character overlays the rope (gap 30px) WHILE CLIMBING (attached,
        # Up held): no-op decision, patrol keeps holding Up.
        worker._climb_state.phase = "climbing-up"
        worker._climb_state.up_held = True
        state.write(True, rope_x=1310.0, char_x=1280.0)
        decision = worker._yolo_rope_action()
        self.assertIsNotNone(decision)
        self.assertIsNone(decision.key)
        self.assertIn("on rope", decision.reason)
        # Idle character at the same gap must JUMP to grab the rope (the
        # original bug: idle + small gap waited forever at layer1).
        worker._climb_state = ClimbState()
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_right")
        # Mid-retry phase (failed grab, Up still held, still near the rope):
        # NOT a no-op - the state machine must keep advancing (verification,
        # retry chord, stall release), otherwise the character freezes.
        worker._climb_state = ClimbState(phase="check-primary-up", up_held=True)
        state.write(True, rope_x=1283.0, char_x=1280.0)  # gap +3px
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_up")
        # Just outside the dead zone while attached (gap 60px): jump resumes.
        worker._climb_state = ClimbState(phase="climbing-up", up_held=True)
        state.write(True, rope_x=1340.0, char_x=1280.0)
        decision = worker._yolo_rope_action()
        self.assertEqual(decision.key, "jump_climb_right")

    def test_yolo_under_rope_jumps_straight_up(self):
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
        # Far enough that a directional jump is required.
        state.write(True, rope_x=1340.0, char_x=1280.0)  # gap +60px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_right")
        # Inside the +-10px center gap (box noise flips the sign): straight
        # up, never a left/right chord.
        state.write(True, rope_x=1283.0, char_x=1280.0)  # gap +3px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_up")
        state.write(True, rope_x=1277.0, char_x=1280.0)  # gap -3px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_up")
        state.write(True, rope_x=1272.0, char_x=1280.0)  # gap -8px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_up")
        # Just outside the center gap: directional jump resumes.
        state.write(True, rope_x=1265.0, char_x=1280.0)  # gap -15px
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_left")

    def test_yolo_box_overlap_forces_straight_up_jump(self):
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
        # Gap 30px (outside the 10px under-rope gap) but the character box
        # horizontally overlaps the rope box: standing right under the rope
        # -> must be a straight-up jump, never a left/right chord.
        state.write(True, rope_x=1360.0, char_x=1330.0,
                    rope_box=(1355.0, 100.0, 1365.0, 500.0),
                    char_box=(1300.0, 600.0, 1360.0, 700.0))
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_up")
        # No overlap and gap outside the under-rope band (but within jump
        # range): directional jump.
        state.write(True, rope_x=1420.0, char_x=1320.0,
                    rope_box=(1415.0, 100.0, 1425.0, 500.0),
                    char_box=(1300.0, 600.0, 1340.0, 700.0))
        self.assertEqual(worker._yolo_rope_action().key, "jump_climb_right")

    def test_climb_failed_cycles_restart_route_at_left_most(self):
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, route_order=[],
        )
        worker._route_phase = "rope"
        # Two failures: still trying at the rope.
        self.assertFalse(worker._climb_cycle_failed())
        self.assertFalse(worker._climb_cycle_failed())
        self.assertEqual(worker._route_phase, "rope")
        # Third consecutive failure: restart patrol from left-most so the
        # character walks away and re-approaches the rope from the edge.
        self.assertTrue(worker._climb_cycle_failed())
        self.assertEqual(worker._route_phase, "left")
        self.assertEqual(worker._climb_state.phase, "idle")
        # A successful walk resets the counter: the next rope approach starts
        # with a fresh budget of attempts.
        worker._climb_failures = 2
        worker._climb_cycle_reset()
        self.assertFalse(worker._climb_cycle_failed())

    def test_climb_failures_escalate_to_self_rescue(self):
        from unittest import mock

        # 同一层爬楼反复失败：多次"重置回最左"后升级为完整自救，避免
        # 无限在同一层巡逻不爬楼。
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={}, route_order=[],
        )
        with mock.patch.object(worker, "_trigger_rescue") as rescue:
            for _restart in range(4):
                for _ in range(3):  # 每次重置需要 3 个失败周期
                    worker._climb_cycle_failed()
            rescue.assert_called_once()
        self.assertEqual(worker._climb_restarts, 0)
        # 爬楼成功后重启计数清零。
        worker._climb_failures = 2
        worker._climb_cycle_reset()
        self.assertEqual(worker._climb_restarts, 0)

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

    def test_attack_defers_to_active_climb(self):
        # Mid-climb (Up held, attached): an active attack must NOT pause/
        # release the climb - releasing Up stopped the character on the rope.
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={},
        )
        worker._climb_state.phase = "climbing-up"
        worker._climb_state.up_held = True
        self.assertTrue(worker._attack_should_defer_to_climb())
        # Mid-grab after a jump (Up held, not attached yet): also defer -
        # releasing Up mid-grab makes the character fall off the rope.
        worker._climb_state.phase = "check-primary-up"
        worker._climb_state.up_held = True
        self.assertTrue(worker._attack_should_defer_to_climb())
        # Idle, Up not held: no deferral - the attack keeps priority and the
        # character fights instead of jumping in place.
        worker._climb_state.phase = "idle"
        worker._climb_state.up_held = False
        self.assertFalse(worker._attack_should_defer_to_climb())

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

    def test_rope_plan_walks_not_jumps_when_climb_not_allowed(self):
        rope_x = .492647
        in_band = MinimapObservation(
            Point(rope_x - .005, .70), None, .9, (0, 0, 1, 1)
        )
        # Fresh YOLO owns the jump: the minimap plan inside the band must
        # only walk (creep), never issue a jump that races the YOLO logic.
        plan = move_towards_rope(in_band, rope_x, .0325, allow_climb=False)
        self.assertEqual(plan.stage, "move-to-rope-edge")
        self.assertEqual(plan.decision.key, "right")
        # With climbing allowed the same position jumps straight up.
        plan = move_towards_rope(in_band, rope_x, .0325, allow_climb=True)
        self.assertEqual(plan.decision.key, "jump_climb_up")

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

    def test_under_rope_up_chord_is_alt_up_not_sideways(self):
        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def press(self, key, duration=0): self.events.append(("press", key, duration)); return True

        sender, state = Sender(), ClimbState()
        start = MinimapObservation(Point(.49, .70), None, .9, (0, 0, 1, 1))
        with patch("movement_worker.time.sleep"):
            result = climb(sender, start, state,
                           preferred_direction="up", persistent_up=True)
        self.assertEqual(result, "up-toward-rope")
        # Alt+Up chord only - never a left/right key.
        downs = [k for kind, k in sender.events if kind == "down"]
        self.assertIn("up", downs)
        self.assertIn("alt", downs)
        self.assertNotIn("left", downs)
        self.assertNotIn("right", downs)

    def test_straight_up_failure_retries_toward_rope_side(self):
        # A failed straight-up jump must retry toward the rope SIDE the
        # character is actually on - never a blind "right" that pushes it
        # away from the rope (character right of rope -> retry LEFT).
        class Sender:
            dry_run = True
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def press(self, key, duration=0): self.events.append(("press", key, duration)); return True

        sender, state = Sender(), ClimbState()
        right_of_rope = MinimapObservation(Point(.505, .70), None, .9, (0, 0, 1, 1))
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, right_of_rope, state,
                                   preferred_direction="up", rope_x=.5),
                             "up-toward-rope")
            # Character right of the rope: the retry must jump LEFT toward it.
            self.assertEqual(climb(sender, right_of_rope, state,
                                   preferred_direction="up", rope_x=.5),
                             "left-retry-toward-rope")

        sender, state = Sender(), ClimbState()
        left_of_rope = MinimapObservation(Point(.495, .70), None, .9, (0, 0, 1, 1))
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, left_of_rope, state,
                                   preferred_direction="up", rope_x=.5),
                             "up-toward-rope")
            # Character left of the rope: the retry must jump RIGHT toward it.
            self.assertEqual(climb(sender, left_of_rope, state,
                                   preferred_direction="up", rope_x=.5),
                             "right-retry-toward-rope")

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
            # Attach needs 2 consecutive rising frames (marker Y up + X
            # aligned): first frame confirms, second commits.
            self.assertEqual(climb(sender, moving, state,
                                   preferred_direction="right", persistent_up=True),
                             "holding-up-awaiting-progress")
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
            # Marker stays centered; world Y advances.  Confirmation needs
            # 2 consecutive frames.
            self.assertEqual(climb(sender, scrolling, state,
                                   preferred_direction="right", persistent_up=True),
                             "holding-up-awaiting-progress")
            self.assertEqual(climb(sender, scrolling, state,
                                   preferred_direction="right", persistent_up=True),
                             "climbing-up")
        self.assertEqual(state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

    def test_arrival_in_progress_suppresses_top_stall(self):
        # At the rope top the world-Y tracker stops advancing; with the
        # worker's layer-confirmation running (arrival_in_progress=True) the
        # stall must NOT fire - Up stays held until the arrival completes.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = set()
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        def make_state():
            state = ClimbState(phase="climbing-up", up_held=True)
            state.baseline_y = .467
            state.baseline_world_y = -.14
            state.last_world_y = -1.0
            state.recent_y = [.467, .45, .43]
            return state

        top = MinimapObservation(
            Point(.48, .43), None, .9, (0, 0, 1, 1),
            world_y_diamonds=-1.0, structure_confidence=.9,
        )
        with patch("movement_worker.time.sleep"):
            # 层确认中：world Y 停滞也不触发 stall，Up 保持。
            sender, state = Sender(), make_state()
            self.assertEqual(climb(sender, top, state, persistent_up=True,
                                   world_y_stall_frames=2,
                                   arrival_in_progress=True),
                             "climbing-up")
            self.assertEqual(climb(sender, top, state, persistent_up=True,
                                   world_y_stall_frames=2,
                                   arrival_in_progress=True),
                             "climbing-up")
            self.assertTrue(state.up_held)
            # 无确认状态：两帧无进展后照常触发 stall 并松开 Up。
            sender, state = Sender(), make_state()
            self.assertEqual(climb(sender, top, state, persistent_up=True,
                                   world_y_stall_frames=2),
                             "climbing-up")
            self.assertEqual(climb(sender, top, state, persistent_up=True,
                                   world_y_stall_frames=2),
                             "climb-stalled-retry")
            self.assertFalse(state.up_held)

    def test_attach_requires_marker_x_aligned_with_rope(self):
        # The reported bug: marker Y/world Y rose, so the old Y-only check
        # declared "attached" while the marker X was far from the rope X
        # (0.50 vs rope 0.38) - the character stood on the ground holding Up
        # forever.  Attach must require the minimap X to be close to the rope.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender, state = Sender(), ClimbState()
        start = MinimapObservation(Point(.50, .70), None, .9, (0, 0, 1, 1))
        rose_but_misaligned = MinimapObservation(
            Point(.50, .66), None, .9, (0, 0, 1, 1)  # X far from rope .38
        )
        with patch("movement_worker.time.sleep"):
            self.assertEqual(climb(sender, start, state,
                                   preferred_direction="right", persistent_up=True,
                                   rope_x=.38),
                             "right-toward-rope")
            for _ in range(6):
                result = climb(sender, rose_but_misaligned, state,
                               preferred_direction="right",
                               persistent_up=True, rope_x=.38)
                # Never "attached" despite the rising Y: X is 0.12 away
                # from the rope, so the character is NOT on it.
                self.assertNotEqual(result, "climbing-up")
        self.assertNotEqual(state.phase, "climbing-up")

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

    def test_stalled_screen_y_releases_up_and_restarts_recovery(self):
        # No world-Y reference (structure_confidence 0): a failed grab fires
        # the attach check mid-jump-arc, the marker falls back, and the Up
        # key must be released so the character jumps again instead of
        # freezing forever holding Up.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up",
            baseline_y=.70,
            up_held=True,
        )
        fell_back = MinimapObservation(
            Point(.48, .70), None, .9, (0, 0, 1, 1)  # marker back at baseline
        )
        self.assertEqual(climb(sender, fell_back, state, persistent_up=True),
                         "climbing-up")
        self.assertEqual(climb(sender, fell_back, state, persistent_up=True),
                         "climb-stalled-retry")
        self.assertEqual(state.phase, "idle")
        self.assertNotIn("up", sender.owned)

    def test_marker_fell_back_from_jump_peak_releases_up_immediately(self):
        # The 4-frame Y window: the marker rose to .64 then fell back (Y
        # increasing again) - the grab failed, the character is NOT on the
        # rope.  Up must be released immediately, not after the stall grace.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up",
            baseline_y=.66,
            up_held=True,
            recent_y=[.66, .64, .645],  # peak .64, now descending
        )
        falling = MinimapObservation(Point(.48, .655), None, .9, (0, 0, 1, 1))
        self.assertEqual(climb(sender, falling, state, persistent_up=True),
                         "climb-stalled-retry")
        self.assertEqual(state.phase, "idle")
        self.assertNotIn("up", sender.owned)
        self.assertEqual(state.recent_y, [])

    def test_marker_still_rising_window_keeps_climbing(self):
        # The window keeps decreasing (still rising): genuinely on the rope,
        # Up stays held.
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up",
            baseline_y=.66,
            up_held=True,
            recent_y=[.66, .65, .64],
        )
        rising = MinimapObservation(Point(.48, .63), None, .9, (0, 0, 1, 1))
        self.assertEqual(climb(sender, rising, state, persistent_up=True),
                         "climbing-up")
        self.assertEqual(state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

    def test_minimap_scroll_marker_y_jump_does_not_false_fell_back(self):
        # Genuine climb: the world Y keeps advancing while the minimap
        # scrolls and the marker Y jumps downward.  The grab must NOT be
        # treated as failed (the old marker-only check released Up and the
        # character fell off the rope right after attaching).
        class Sender:
            dry_run = True
            def __init__(self): self.owned = {"up"}
            def key_down(self, key): self.owned.add(key); return True
            def key_up(self, key): self.owned.discard(key); return True
            def press(self, key, duration=0): return True

        sender = Sender()
        state = ClimbState(
            phase="climbing-up",
            baseline_y=.587891,
            baseline_world_y=11.322577,
            up_held=True,
            recent_y=[.60, .587891],
            last_world_y=11.0,
        )
        scrolled = MinimapObservation(
            Point(.092375, .607422), None, .9, (0, 0, 1, 1),
            world_y_diamonds=10.48, structure_confidence=.9,
        )
        # Marker dropped 0.0195 (would trip the old check) but the world Y
        # advanced 0.84: still climbing, Up stays held.
        self.assertEqual(climb(sender, scrolled, state, persistent_up=True),
                         "climbing-up")
        self.assertEqual(state.phase, "climbing-up")
        self.assertIn("up", sender.owned)

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

        with patch("movement_worker.time.monotonic", side_effect=[0, 1.05, 1.05]):
            for _ in range(3):
                self.assertEqual(worker._resync_route_layer(upper), "layer1")
            self.assertEqual(sender.released, [])
            self.assertEqual(worker._climb_state.phase, "arrival-compensation")
            self.assertEqual(worker._resync_route_layer(y_flicker), "layer2")
        self.assertEqual(sender.released, ["up"])
        # The climb-arrival stamp is set so stair jumps are suppressed while
        # the character settles on the platform edge.
        self.assertIsNotNone(worker._climb_arrival_at)

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

    def test_aligned_marker_jumps_straight_up(self):
        rope_x = .492647
        aligned = MinimapObservation(Point(rope_x, .70), None, .9, (0, 0, 1, 1))
        # Exactly under the rope: straight-up jump, never a left/right chord.
        self.assertEqual(
            move_towards_rope(aligned, rope_x, .032500).decision.key,
            "jump_climb_up",
        )
        # Inside the +-0.01 center gap: still straight up.
        close = MinimapObservation(Point(rope_x - .005, .70), None, .9,
                                   (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(close, rope_x, .032500).decision.key,
            "jump_climb_up",
        )
        # Outside the center gap (0.012): normal directional jump.
        off = MinimapObservation(Point(rope_x - .012, .70), None, .9,
                                 (0, 0, 1, 1))
        self.assertEqual(
            move_towards_rope(off, rope_x, .032500).decision.key,
            "jump_climb_right",
        )

    def test_layer_route_left_right_then_rope(self):
        positions = {
            "layer1": {
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
                "rope_pos": {"x": .5, "y": .7},
            },
            "layer2": {
                "left_most_pos": {"x": .3, "y": .5},
                "right_most_pos": {"x": .6, "y": .5},
            },
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            fixed_target_x=.5, important_positions=positions,
            route_order=["layer1", "layer2"],
        )
        worker._route_layer_index = 0
        at_left = MinimapObservation(Point(.2, .7), None, .9, (0, 0, 1, 1))
        target, is_rope, label = worker._route_target(at_left)
        self.assertEqual((target, is_rope, label), (.2, False, "layer1.left-most"))
        self.assertTrue(worker._advance_route_endpoint(at_left, target))
        target, is_rope, label = worker._route_target(at_left)
        self.assertEqual((target, is_rope, label), (.8, False, "layer1.right-most"))
        at_right = MinimapObservation(Point(.8, .7), None, .9, (0, 0, 1, 1))
        self.assertTrue(worker._advance_route_endpoint(at_right, target))
        self.assertEqual(worker._route_target(at_right), (.5, True, "layer1.rope"))

    def test_rope_only_layer_goes_directly_to_rope(self):
        positions = {
            "layer1": {"rope_pos": {"x": .5, "y": .7}},
            "layer2": {"rope_pos": {"x": .6, "y": .5}},
        }
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            fixed_target_x=.5, important_positions=positions,
            route_order=["layer1", "layer2"],
        )
        worker._route_layer_index = 0
        at_bottom = MinimapObservation(Point(.6, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(worker._route_target(at_bottom),
                         (.5, True, "layer1.rope"))

    def test_left_only_layer_repeats_at_left(self):
        positions = {"layer1": {"left_most_pos": {"x": .2, "y": .7}}}
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            fixed_target_x=.5, important_positions=positions,
            route_order=["layer1"],
        )
        worker._route_layer_index = 0
        at_left = MinimapObservation(Point(.2, .7), None, .9, (0, 0, 1, 1))
        target, is_rope, label = worker._route_target(at_left)
        self.assertEqual((target, is_rope, label), (.2, False, "layer1.left-most"))
        self.assertTrue(worker._advance_route_endpoint(at_left, target))
        # No further action on the layer: it repeats at left.
        self.assertEqual(worker._route_phase, "left")

    def test_empty_route_stands_still(self):
        # Nothing recorded: the worker holds position (Fixed Attack / YOLO
        # keep attacking) instead of falling back to a rope walk.
        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            important_positions={},
        )
        obs = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        self.assertEqual(worker._route_target(obs), (None, False, "stand-still"))

    def test_route_sync_is_bottom_up_including_newly_recorded_layers(self):
        # A lower layer recorded later (or with only partial points) must not
        # become the "final" layer: the route stays bottom-up by numeric
        # suffix, otherwise its rope is hidden and the character stands still
        # after climbing onto it.
        from patrol_control import PatrolSnapshot

        class FakeController:
            def snapshot(self, coordinate_layout=None):
                return PatrolSnapshot(
                    enabled=True,
                    selected_layer="layer3",
                    route_order=("layer3", "layer4"),
                    layers={
                        "layer1": {
                            "right_most_pos": {"x": .4, "y": .69},
                            "rope_pos": {"x": .2, "y": .69},
                        },
                        "layer2": {"rope_pos": {"x": .2, "y": .53}},
                        "layer3": {
                            "left_most_pos": {"x": .1, "y": .42},
                            "right_most_pos": {"x": .4, "y": .42},
                            "rope_pos": {"x": .16, "y": .42},
                        },
                        "layer4": {
                            "left_most_pos": {"x": .1, "y": .15},
                            "right_most_pos": {"x": .24, "y": .15},
                        },
                    },
                    climbing_enabled=True,
                    final_layer_action="drop_to_first_layer",
                )

        worker = MovementWorker(
            queue.Queue(), object(), threading.Event(),
            patrol_controller=FakeController(),
        )
        worker._sync_patrol_controller()
        self.assertEqual(
            worker._route_layers, ["layer1", "layer2", "layer3", "layer4"]
        )

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

    def test_map_profile_route_order_is_normalized_bottom_up(self):
        # Even when the recorded route_order is scrambled (top layer recorded
        # first, e.g. "Add Layer" auto-selects the new layer), the route is
        # always bottom-up by layer number - layer1 can never patrol above
        # layer2.
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
        self.assertEqual(worker._route_layers, ["layer1", "layer2"])

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
        aligned = MinimapObservation(Point(.5, .7), Point(.507, .7), .9, (0, 0, 1, 1))
        self.assertEqual(plan_movement(left).key, "left")
        self.assertEqual(plan_movement(left).duration, 2.0)
        # Gap .007 is inside the +-0.008 under-rope gap: straight up, not right.
        self.assertEqual(plan_movement(aligned).key, "jump_climb_up")

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
        # No rope recorded on this single layer: it repeats at left.
        self.assertEqual(worker._route_phase, "left")

    def test_saved_rope_position_overrides_connector_detection(self):
        observation = MinimapObservation(Point(.70, .70), None, .90, (0, 0, 1, 1))
        self.assertEqual(plan_movement(observation, fixed_target_x=.25).key, "left")
        aligned = MinimapObservation(Point(.251, .70), None, .90, (0, 0, 1, 1))
        self.assertEqual(plan_movement(aligned, fixed_target_x=.25).key,
                         "jump_climb_up")

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
        # Inside the tight rope range the character is under the rope:
        # the jump is straight up, not a right chord.
        self.assertEqual(plan_movement(
            inside, fixed_target_x=target, horizontal_tolerance=.0025,
            final_calculation_distance=.0025).key, "jump_climb_up")


class StairJumpTests(unittest.TestCase):
    """Stairs that block the left/right walk are jumped automatically."""

    def _worker(self, **kwargs):
        positions = {
            "layer1": {
                "left_most_pos": {"x": .2, "y": .7},
                "right_most_pos": {"x": .8, "y": .7},
            }
        }
        defaults = dict(
            frame_queue=queue.Queue(),
            key_sender=object(),
            stop_event=threading.Event(),
            fixed_target_x=.5,
            important_positions=positions,
            route_order=["layer1"],
            stair_jump_stall_frames=2,
            stair_jump_attempts_max=3,
        )
        defaults.update(kwargs)
        return MovementWorker(**defaults)

    def _plan(self, px, direction, reached=False):
        if direction == "right":
            target = Point(.8, .7)
            gap = .8 - px
        else:
            target = Point(.2, .7)
            gap = .2 - px
        return PositionMovementPlan(
            f"move-to-{direction}", Point(px, .7), target, gap, reached,
            MovementDecision(None if reached else direction, "walk", 2.0),
        )

    def test_stuck_while_walking_jumps_automatically(self):
        # No jump points are recorded anywhere: a stuck marker (walk hold
        # issued but X not advancing) is enough to trigger the stair jump.
        worker = self._worker()
        worker._route_layer_index = 0
        worker._route_phase = "right"
        now = 100.0
        stuck = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        # Frame 1: no previous X yet -> one stall frame, still below the
        # confirmation count.
        self.assertIsNone(worker._stair_jump_decision(
            stuck, "layer1.right-most", self._plan(.5, "right"), now))
        # Frame 2: marker still not advancing -> confirmed stuck -> jump.
        decision = worker._stair_jump_decision(
            stuck, "layer1.right-most", self._plan(.5, "right"), now + 2.5)
        self.assertEqual(decision.key, "stair_jump_right")
        self.assertEqual(worker._stair_state["attempts"], 1)

    def test_stair_jump_suppressed_during_patrol_start_grace(self):
        # Right after Start Patrol the character stands still - that is not
        # being stuck at a stair.  No stair jump until the grace window ends.
        worker = self._worker(patrol_start_grace_seconds=3.0)
        worker._route_layer_index = 0
        worker._route_phase = "right"
        now = 100.0
        worker._patrol_started_at = now
        stuck = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        plan = self._plan(.5, "right")
        for offset in (0.0, 2.5):
            self.assertIsNone(worker._stair_jump_decision(
                stuck, "layer1.right-most", plan, now + offset))
        # After the grace window the stall counter starts fresh: one confirm
        # frame, then a genuine stall jumps.
        self.assertIsNone(worker._stair_jump_decision(
            stuck, "layer1.right-most", plan, now + 4.0))
        decision = worker._stair_jump_decision(
            stuck, "layer1.right-most", plan, now + 6.5)
        self.assertEqual(decision.key, "stair_jump_right")

    def test_moving_marker_never_jumps(self):
        worker = self._worker()
        worker._route_layer_index = 0
        worker._route_phase = "right"
        now = 100.0
        # Marker advancing: the stall counter keeps resetting.
        for index, px in enumerate((.44, .46, .48, .50)):
            moving = MinimapObservation(Point(px, .7), None, .9, (0, 0, 1, 1))
            self.assertIsNone(worker._stair_jump_decision(
                moving, "layer1.right-most", self._plan(px, "right"),
                now + index))
        self.assertEqual(worker._stair_state["stall_frames"], 0)

    def test_stuck_jumps_only_in_left_right_phases(self):
        # The stair jump belongs to move-to-left-most / move-to-right-most
        # only - a rope-phase label never produces a stair jump.
        worker = self._worker()
        worker._route_layer_index = 0
        worker._route_phase = "rope"
        stuck = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        for frame in range(4):
            self.assertIsNone(worker._stair_jump_decision(
                stuck, "layer1.rope", self._plan(.5, "right"), 100.0 + frame))

    def test_grace_period_and_attempts_cap_stop_hop_in_place(self):
        worker = self._worker(stair_jump_stall_frames=1,
                              stair_jump_attempts_max=2)
        worker._route_layer_index = 0
        worker._route_phase = "right"
        stuck = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        plan = self._plan(.5, "right")
        jump1 = worker._stair_jump_decision(stuck, "layer1.right-most", plan, 10.0)
        self.assertEqual(jump1.key, "stair_jump_right")
        # Inside the grace window no second jump is issued.
        self.assertIsNone(worker._stair_jump_decision(
            stuck, "layer1.right-most", plan, 10.5))
        # After grace the second (final) jump fires.
        jump2 = worker._stair_jump_decision(
            stuck, "layer1.right-most", plan, 13.0)
        self.assertEqual(jump2.key, "stair_jump_right")
        self.assertEqual(worker._stair_state["attempts"], 2)
        # Attempts exhausted: no more jumps; the boundary is treated as
        # unreachable and the phase is force-advanced (loops to the next
        # recorded phase instead of pressing direction forever).
        self.assertIsNone(worker._stair_jump_decision(
            stuck, "layer1.right-most", plan, 16.0))
        self.assertTrue(worker._stair_state["gave_up"])
        self.assertEqual(worker._route_phase, "left")

    def test_phase_change_resets_stair_state(self):
        worker = self._worker()
        worker._route_layer_index = 0
        worker._route_phase = "right"
        stuck = MinimapObservation(Point(.5, .7), None, .9, (0, 0, 1, 1))
        worker._stair_jump_decision(
            stuck, "layer1.right-most", self._plan(.5, "right"), 1.0)
        worker._stair_jump_decision(
            stuck, "layer1.right-most", self._plan(.5, "right"), 3.5)
        self.assertEqual(worker._stair_state["attempts"], 1)
        # A new phase label starts a fresh approach with a clean budget.
        worker._route_phase = "left"
        worker._stair_jump_decision(
            stuck, "layer1.left-most", self._plan(.5, "left"), 6.0)
        self.assertEqual(worker._stair_state["phase_label"], "layer1.left-most")
        self.assertEqual(worker._stair_state["attempts"], 0)
        self.assertEqual(worker._stair_state["stall_frames"], 1)

    def test_send_stair_jump_holds_direction_and_taps_alt(self):
        class Sender:
            dry_run = False
            def __init__(self): self.events = []
            def key_down(self, key): self.events.append(("down", key)); return True
            def key_up(self, key): self.events.append(("up", key)); return True
            def is_target_focused(self): return True

        sender = Sender()
        worker = MovementWorker(
            queue.Queue(), sender, threading.Event(), important_positions={},
        )
        worker.stair_jump_lead_seconds = 0.0
        worker.stair_jump_alt_hold_seconds = 0.01
        ok = worker._send_stair_jump(
            MovementDecision("stair_jump_right", "stuck at stair", 0.05))
        self.assertTrue(ok)
        self.assertEqual(
            sender.events,
            [("down", "right"), ("down", "alt"), ("up", "alt"), ("up", "right")],
        )

    def test_stair_jump_is_treated_as_walking_by_pickup_gate(self):
        worker = self._worker()
        self.assertTrue(worker._is_walk_key("stair_jump_left"))
        self.assertTrue(worker._is_walk_key("right"))
        self.assertFalse(worker._is_walk_key("climb"))
        self.assertFalse(worker._is_walk_key(None))

    def test_patrol_facing_is_none_safe_for_noop_decision(self):
        # A no-op "wait" decision (key=None) during a climb/rope approach must
        # NOT crash the facing tracking - this previously raised
        # AttributeError on decision.key.startswith("stair_jump_") and froze
        # the movement worker every frame.
        worker = self._worker()
        self.assertIsNone(worker._patrol_facing_for_key(None))
        self.assertIsNone(worker._patrol_facing_for_key("climb"))
        self.assertIsNone(worker._patrol_facing_for_key("jump_climb_up"))
        self.assertEqual(worker._patrol_facing_for_key("left"), "left")
        self.assertEqual(worker._patrol_facing_for_key("right"), "right")
        self.assertEqual(
            worker._patrol_facing_for_key("stair_jump_right"), "right"
        )
        self.assertEqual(
            worker._patrol_facing_for_key("jump_climb_left"), "left"
        )


if __name__ == "__main__":
    unittest.main()
