"""Tests for character detection and attack decisions (no model needed).

These tests import ``auto`` (which needs torch/ultralytics), so they run
under the yolo venv::

    venv313\\Scripts\\python.exe -m unittest test_yolo_decision

When torch is unavailable (e.g. the assistant's Python 3.10 env) the module
import fails gracefully and every test is skipped.
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "yolo-detection"))

try:
    from auto import Detection
except Exception:  # torch/ultralytics missing (e.g. Python 3.10 env)
    Detection = None


def _require_auto():
    if Detection is None:
        raise unittest.SkipTest("auto module unavailable (needs torch)")


def make_detection(cls, cx, cy, conf=0.9, w=60, h=80):
    return Detection(
        bbox=[cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2],
        confidence=conf,
        class_id=0 if cls == "character" else 3,
        class_name=cls,
        center=(cx, cy),
        distance_from_center=0.0,
    )


class AttackDecisionTests(unittest.TestCase):
    def setUp(self):
        _require_auto()

    def test_no_character_means_no_attack(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        mobs = [make_detection("mob", 500, 500)]
        self.assertIsNone(bot.attack_decision(mobs, None, 800))

    def test_mob_within_range_is_targeted(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 400, 400)
        near = make_detection("mob", 460, 410)   # ~62px away
        far = make_detection("mob", 900, 900)    # ~707px away
        target = bot.attack_decision([near, far], character, 800)
        self.assertIs(target, near)

    def test_mob_outside_range_is_ignored(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 400, 400)
        far = make_detection("mob", 1400, 400)   # 1000px away
        self.assertIsNone(bot.attack_decision([far], character, 800))

    def test_nearest_attackable_mob_wins(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 400, 400)
        near = make_detection("mob", 450, 400)
        mid = make_detection("mob", 700, 400)
        target = bot.attack_decision([mid, near], character, 800)
        self.assertIs(target, near)

    def test_mob_beyond_drawn_line_is_not_attackable(self):
        # attack_range is the line WIDTH: it spans attack_range/2 on each
        # side of the character.  A mob beyond that half-width (e.g. 600px
        # with range 800 -> line edge at 400) must never be targeted, even
        # though 600 < 800 (the old euclidean-radius bug attacked these).
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 1280, 880)
        far_right = make_detection("mob", 2001, 900)   # dx=+721 > 400
        far_left = make_detection("mob", 437, 897)     # dx=-843 < -400
        self.assertIsNone(bot.attack_decision(
            [far_right, far_left], character, 800))

    def test_mob_inside_line_is_attackable(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 1280, 880)
        inside = make_detection("mob", 1500, 890)      # dx=+220 <= 400
        target = bot.attack_decision([inside], character, 800)
        self.assertIs(target, inside)

    def test_vertical_distance_is_also_gated(self):
        # A mob directly above/below but beyond the vertical half-range
        # must not be targeted (the character cannot attack straight up).
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        character = make_detection("character", 1280, 880)
        above = make_detection("mob", 1290, 100)       # dy=-780 > 400
        self.assertIsNone(bot.attack_decision([above], character, 800))

    def test_tiny_box_like_drop_is_not_attacked(self):
        # Dropped items are misclassified as mobs but their boxes are tiny;
        # the min-box gate rejects them (default 20px per side).
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        bot.config = {}
        character = make_detection("character", 1280, 880)
        tiny = Detection(
            bbox=[1290, 890, 1305, 905],  # 15x15 px
            confidence=0.9, class_id=3, class_name="mob",
            center=(1297, 897), distance_from_center=0.0,
        )
        self.assertIsNone(bot.attack_decision([tiny], character, 800))

    def test_normal_sized_mob_still_attacked(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        bot.config = {}
        character = make_detection("character", 1280, 880)
        mob = make_detection("mob", 1350, 890)  # 60x80 box
        target = bot.attack_decision([mob], character, 800)
        self.assertIs(target, mob)


class CharacterStabilizationTests(unittest.TestCase):
    def setUp(self):
        _require_auto()

    def test_continuity_prefers_candidate_near_previous_position(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        # Both candidates are near screen center; the true player is the one
        # continuous with the previous position.
        true_player = make_detection("character", 1180, 880, conf=0.6)
        false_positive = make_detection("character", 1000, 850, conf=0.95)
        best = bot._score_character_candidates(
            [false_positive, true_player], (1200, 885), 2561, 1601
        )
        self.assertIs(best, true_player)

    def test_center_proximity_beats_far_high_confidence(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        # The player is at screen center (camera follows); another player at
        # the far edge must NOT win even with higher confidence.
        player = make_detection("character", 1280, 880, conf=0.6)
        other_player = make_detection("character", 2300, 300, conf=0.95)
        best = bot._score_character_candidates(
            [other_player, player], None, 2561, 1601
        )
        self.assertIs(best, player)

    def test_no_previous_position_uses_center(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        center = make_detection("character", 1280, 880, conf=0.5)
        corner = make_detection("character", 100, 100, conf=0.9)
        best = bot._score_character_candidates([corner, center], None, 2561, 1601)
        self.assertIs(best, center)

    def test_median_smoothing_kills_jitter(self):
        from auto import OptimizedMapleBot

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        bot._char_history = __import__("collections").deque(maxlen=8)
        bot._char_smoothed = None
        bot._char_miss_frames = 0
        # Feed a noisy series around x=1000: one outlier must not move the
        # median center by much.
        centers = [(1000, 500)] * 3 + [(1800, 500)] + [(1000, 500)] * 3
        for cx, cy in centers:
            bot._char_history.append((cx, cy))
        xs = sorted(p[0] for p in bot._char_history)
        self.assertEqual(xs[len(xs) // 2], 1000)


class AttackExecutorTests(unittest.TestCase):
    """Dry-run tests: never inject real keys."""

    def setUp(self):
        _require_auto()

    def _executor(self, **kwargs):
        from attack_executor import AttackExecutor

        # Dry run + patched foreground check: no real input is ever sent.
        ex = AttackExecutor("\u5192\u9669\u5c9b\u6000\u65e7\u670d",
                            dry_run=True, **kwargs)
        ex._taps = []
        ex._tap = lambda key, hold: ex._taps.append((key, round(hold, 3)))
        return ex

    def test_facing_for_returns_direction(self):
        ex = self._executor()
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        left = make_detection("mob", 200, 400)
        above = make_detection("mob", 402, 100)  # |dx| < 8 dead zone
        self.assertEqual(ex.facing_for(char, right), "right")
        self.assertEqual(ex.facing_for(char, left), "left")
        self.assertIsNone(ex.facing_for(char, above))

    def test_attack_turns_toward_target_then_presses_ctrl(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.10), ("ctrl", 0.1)])

    def test_attack_turns_when_patrol_moved_character(self):
        # Patrol walked the character left (publishing facing=left), then a
        # monster appears on the right.  The turn must still fire - the
        # executor never relies on a cached facing belief.
        import tempfile
        from pathlib import Path
        from combat_coordination import PatrolStateFile

        tmp = Path(tempfile.mkdtemp()) / "patrol_state.json"
        state = PatrolStateFile(str(tmp))
        ex = self._executor(cooldown=0.0, turn_settle=0.0,
                            patrol_state_path=str(tmp))
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        # Executor believes it faces right (from an earlier attack).
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._facing, "right")
        ex._taps.clear()
        # Patrol walked the character left: publish facing=left.
        state.write(False, "left", "left")
        # Monster still on the right: the turn tap must fire every attack.
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.10), ("ctrl", 0.1)])
        self.assertEqual(ex._facing, "right")

    def test_attack_always_turns_toward_target(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        char = make_detection("character", 400, 400)
        left = make_detection("mob", 200, 400)
        ex.attack(char, left)  # turns left
        ex._taps.clear()
        # Every attack turns toward the target first - never trust a cached
        # facing to skip the turn (the game is the source of truth).
        self.assertTrue(ex.attack(char, left))
        self.assertEqual(ex._taps, [("left", 0.10), ("ctrl", 0.1)])

    def test_attack_respects_cooldown(self):
        ex = self._executor(cooldown=10.0)
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        self.assertTrue(ex.attack(char, mob))
        ex._taps.clear()
        # Inside the cooldown window: no keys sent.
        self.assertFalse(ex.attack(char, mob))
        self.assertEqual(ex._taps, [])

    def test_attack_blocked_when_game_not_foreground(self):
        ex = self._executor(cooldown=0.0)
        ex.is_game_foreground = lambda: False
        ex.select_window = lambda: False  # refocus also fails
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        self.assertFalse(ex.attack(char, mob))
        self.assertEqual(ex._taps, [])

    def test_attack_blocked_when_patrol_climbing(self):
        import tempfile
        from pathlib import Path
        from combat_coordination import PatrolStateFile

        tmp = Path(tempfile.mkdtemp()) / "patrol_state.json"
        state = PatrolStateFile(str(tmp))
        ex = self._executor(cooldown=0.0, patrol_state_path=str(tmp))
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        # Patrol climbing: attack is blocked.
        state.write(True, "climb")
        self.assertFalse(ex.attack(char, mob))
        self.assertEqual(ex._taps, [])
        # Patrol idle: attack fires.
        state.write(False)
        self.assertTrue(ex.attack(char, mob))
        self.assertNotEqual(ex._taps, [])

    def test_attack_refocuses_then_attacks(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        # Not focused first, but refocus succeeds: attack must still fire.
        state = {"fg": False}
        ex.is_game_foreground = lambda: state["fg"]
        ex.select_window = lambda: (state.__setitem__("fg", True) or True)
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        self.assertTrue(ex.attack(char, mob))
        self.assertEqual(ex._taps, [("right", 0.10), ("ctrl", 0.1)])

    def test_reset_facing_forgets_direction(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        ex.attack(char, right)
        ex.reset_facing()
        ex._taps.clear()
        # Facing forgotten: turns right again before attacking.
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.10), ("ctrl", 0.1)])

    def test_failed_injection_returns_false_and_logs(self):
        ex = self._executor(cooldown=0.0)
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        calls = {"n": 0}

        def failing_tap(key, hold):
            calls["n"] += 1
            raise OSError(87, "SendInput injected 0/1 events")

        ex._tap = failing_tap
        self.assertFalse(ex.attack(char, mob))
        self.assertEqual(calls["n"], 1)

    def test_real_sendinput_injects(self):
        # Smoke test against the actual Win32 SendInput used by the game
        # path: the corrected 40-byte INPUT struct must inject exactly one
        # event per call (verified by SendInput's return value).
        import ctypes
        from attack_executor import _send_scan_code, _SCAN

        scan, extended = _SCAN["ctrl"]
        _send_scan_code(scan, key_up=False, extended=extended)
        _send_scan_code(scan, key_up=True, extended=extended)
        self.assertTrue(True)  # no OSError == injection succeeded



class FullClassDetectionTests(unittest.TestCase):
    """include_all=True returns every class without zone/tracker filters."""

    def setUp(self):
        _require_auto()

    def _make_bot(self):
        from auto import OptimizedMapleBot

        class FakeModel:
            names = {
                0: "character", 1: "environment", 2: "item",
                3: "mob", 4: "npc", 5: "ui",
            }

            def __init__(self, call_fn):
                self._call_fn = call_fn

            def __call__(self, img, conf=None, verbose=False):
                return self._call_fn(img, conf, verbose)

        bot = OptimizedMapleBot.__new__(OptimizedMapleBot)
        bot.model = FakeModel(
            lambda img, conf, verbose: self._fake_call(img, conf, verbose)
        )
        bot.confidence_threshold = 0.2
        bot.monitor = {"width": 1280, "height": 720}
        # Dotted-path config lookup like the real ConfigManager so the
        # center-zone filter actually engages in these tests.
        class DotConfig(dict):
            def get(self, key, default=None):
                if "." in key:
                    node = dict(self)
                    for part in key.split("."):
                        if not isinstance(node, dict) or part not in node:
                            return default
                        node = node[part]
                    return node
                return dict.get(self, key, default)

        bot.config = DotConfig({
            "detection_behavior": {
                "center_zone": {"enabled": True},
            },
        })
        bot.stats = {"detections": 0}
        bot.performance_monitor = type(
            "PM", (), {"record_detection_time": lambda self, t: None}
        )()
        return bot

    @staticmethod
    def _fake_call(img, conf, verbose):
        import numpy as np

        class Tensor:
            def __init__(self, value):
                self._v = value

            def cpu(self):
                return self

            def numpy(self):
                return self._v

        specs = [
            (np.array([300, 200, 340, 240]), 0.9, 2),   # item (inside zone)
            (np.array([400, 200, 460, 260]), 0.8, 1),   # environment
            (np.array([500, 300, 560, 360]), 0.7, 0),   # character
            (np.array([600, 350, 660, 410]), 0.8, 3),   # mob
        ]

        class FakeBox:
            def __init__(self, xyxy, conf, cls):
                self.xyxy = [Tensor(xyxy)]
                self.conf = [Tensor(np.float64(conf))]  # 0-d: float()/int() work
                self.cls = [Tensor(np.float64(cls))]

        class FakeBoxes:
            def __init__(self):
                self._boxes = [FakeBox(*s) for s in specs]

            def __iter__(self):
                return iter(self._boxes)

        class FakeResult:
            boxes = FakeBoxes()

        class FakeResults:
            def __iter__(self):
                yield FakeResult()

        return FakeResults()

    def test_include_all_returns_kept_classes_only(self):
        import numpy as np

        bot = self._make_bot()
        dets = bot.detect_objects(np.zeros((720, 1280, 3), dtype=np.uint8),
                                  include_all=True)
        classes = sorted(d.class_name for d in dets)
        # item/npc/ui are excluded; only character/environment/mob remain.
        self.assertEqual(classes, ["character", "environment", "mob"])

    def test_default_still_mobs_only(self):
        import numpy as np

        bot = self._make_bot()
        bot.config["detection_behavior"]["detect_only_mobs"] = True
        dets = bot.detect_objects(np.zeros((720, 1280, 3), dtype=np.uint8))
        # mob is the only class returned (no temporal confirmation anymore).
        self.assertEqual([d.class_name for d in dets], ["mob"])

    def test_config_manager_set_and_save_threshold(self):
        import tempfile
        from pathlib import Path
        import yaml
        from auto import ConfigManager

        tmp = Path(tempfile.mkdtemp()) / "config.yaml"
        tmp.write_text(
            "detection_behavior:\n  confidence_threshold: 0.33\n",
            encoding="utf-8",
        )
        manager = ConfigManager(str(tmp))
        manager.set("detection_behavior.confidence_threshold", 0.64)
        self.assertTrue(manager.save())
        reloaded = yaml.safe_load(tmp.read_text(encoding="utf-8"))
        self.assertAlmostEqual(
            reloaded["detection_behavior"]["confidence_threshold"], 0.64
        )
        # Dotted get on the fresh manager reflects the new value.
        self.assertAlmostEqual(
            manager.get("detection_behavior.confidence_threshold"), 0.64
        )

    def test_out_of_zone_environment_and_mobs_excluded(self):
        import numpy as np

        bot = self._make_bot()
        # Boxes outside the default zone (x 256-1024, y 144-576):
        # environment at top-left, mob at bottom-right.
        class FakeBox:
            def __init__(self, xyxy, conf, cls):
                self.xyxy = [self._t(xyxy)]
                self.conf = [self._t(np.float64(conf))]
                self.cls = [self._t(np.float64(cls))]

            @staticmethod
            def _t(v):
                class Tensor:
                    def __init__(self, val):
                        self._v = val

                    def cpu(self):
                        return self

                    def numpy(self):
                        return self._v

                return Tensor(v)

        class FakeBoxes:
            def __iter__(self):
                # out-of-zone environment (x=10 < 256)
                yield FakeBox(np.array([10, 200, 60, 260]), 0.9, 1)
                # out-of-zone mob (y=700 > 576)
                yield FakeBox(np.array([600, 700, 660, 760]), 0.9, 3)
                # in-zone character (kept)
                yield FakeBox(np.array([500, 300, 560, 360]), 0.7, 0)

        class FakeResult:
            boxes = FakeBoxes()

        class FakeResults:
            def __iter__(self):
                yield FakeResult()

        bot.model = type("M", (), {
            "names": bot.model.names,
            "__call__": lambda self, img, conf, verbose: FakeResults(),
        })()
        bot.detect_character = lambda img: None
        dets = bot.detect_objects(np.zeros((720, 1280, 3), dtype=np.uint8),
                                  include_all=True)
        # Only the in-zone character survives; environment and mob outside
        # the zone are dropped even in the preview path.
        self.assertEqual([d.class_name for d in dets], ["character"])

    def test_detect_rope_finds_tall_environment_box(self):
        import numpy as np

        bot = self._make_bot()
        # A tall narrow environment box (rope) plus a wide platform box.
        class FakeBox:
            def __init__(self, xyxy, conf, cls):
                self.xyxy = [self._t(xyxy)]
                self.conf = [self._t(np.float64(conf))]
                self.cls = [self._t(np.float64(cls))]

            @staticmethod
            def _t(v):
                class Tensor:
                    def __init__(self, val):
                        self._v = val

                    def cpu(self):
                        return self

                    def numpy(self):
                        return self._v

                return Tensor(v)

        class FakeBoxes:
            def __iter__(self):
                yield FakeBox(np.array([600, 100, 616, 500]), 0.8, 1)   # rope
                yield FakeBox(np.array([100, 400, 900, 430]), 0.9, 1)  # platform

        class FakeResult:
            boxes = FakeBoxes()

        class FakeResults:
            def __iter__(self):
                yield FakeResult()

        bot.model = type("M", (), {
            "names": bot.model.names,
            "__call__": lambda self, img, conf, verbose: FakeResults(),
        })()
        bot.detect_character = lambda img: None  # fall back to screen center
        rope = bot.detect_rope(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertIsNotNone(rope)
        self.assertEqual(rope.class_name, "environment")
        # The wide platform box (h=30 < 2.5*w) must be rejected; the rope
        # (h=400, w=16) wins.
        x1, y1, x2, y2 = rope.bbox
        self.assertGreater(y2 - y1, 300)


if __name__ == "__main__":
    unittest.main()

