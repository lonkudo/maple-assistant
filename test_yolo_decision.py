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
        ex = self._executor(cooldown=0.0)
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.05), ("ctrl", 0.08)])

    def test_attack_skips_turn_when_already_facing(self):
        ex = self._executor(cooldown=0.0)
        char = make_detection("character", 400, 400)
        left = make_detection("mob", 200, 400)
        ex.attack(char, left)  # turns left
        ex._taps.clear()
        # Still on the left: only ctrl, no extra turn tap.
        self.assertTrue(ex.attack(char, left))
        self.assertEqual(ex._taps, [("ctrl", 0.08)])

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
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        self.assertFalse(ex.attack(char, mob))
        self.assertEqual(ex._taps, [])

    def test_reset_facing_forgets_direction(self):
        ex = self._executor(cooldown=0.0)
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        ex.attack(char, right)
        ex.reset_facing()
        ex._taps.clear()
        # Facing forgotten: turns right again before attacking.
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.05), ("ctrl", 0.08)])


if __name__ == "__main__":
    unittest.main()
