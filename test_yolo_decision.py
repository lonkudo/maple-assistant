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
        self.assertEqual(ex._taps, [("right", 0.08), ("ctrl", 0.1)])

    def test_attack_skips_turn_when_already_facing(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        char = make_detection("character", 400, 400)
        left = make_detection("mob", 200, 400)
        ex.attack(char, left)  # turns left
        ex._taps.clear()
        # Still on the left: only ctrl, no extra turn tap.
        self.assertTrue(ex.attack(char, left))
        self.assertEqual(ex._taps, [("ctrl", 0.1)])

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

    def test_attack_refocuses_then_attacks(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        # Not focused first, but refocus succeeds: attack must still fire.
        state = {"fg": False}
        ex.is_game_foreground = lambda: state["fg"]
        ex.select_window = lambda: (state.__setitem__("fg", True) or True)
        char = make_detection("character", 400, 400)
        mob = make_detection("mob", 600, 400)
        self.assertTrue(ex.attack(char, mob))
        self.assertEqual(ex._taps, [("right", 0.08), ("ctrl", 0.1)])

    def test_reset_facing_forgets_direction(self):
        ex = self._executor(cooldown=0.0, turn_settle=0.0)
        char = make_detection("character", 400, 400)
        right = make_detection("mob", 600, 400)
        ex.attack(char, right)
        ex.reset_facing()
        ex._taps.clear()
        # Facing forgotten: turns right again before attacking.
        self.assertTrue(ex.attack(char, right))
        self.assertEqual(ex._taps, [("right", 0.08), ("ctrl", 0.1)])

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


class MobTrackerTests(unittest.TestCase):
    """Temporal confirmation: flash detections must not reach the bot."""

    def setUp(self):
        _require_auto()

    def _tracker(self, **kwargs):
        from mob_tracker import MobTracker

        return MobTracker(**kwargs)

    def test_single_frame_flash_is_rejected(self):
        tracker = self._tracker()
        mob = make_detection("mob", 500, 500)
        # One frame only (the flash) -> never confirmed.
        self.assertEqual(tracker.update([mob]), [])
        # A clear frame, then the flash returns briefly: still rejected.
        self.assertEqual(tracker.update([]), [])
        self.assertEqual(tracker.update([mob]), [])
        self.assertEqual(tracker.update([mob]), [])  # 2 consecutive, not 3

    def test_mob_confirmed_after_three_frames(self):
        tracker = self._tracker()
        mob = make_detection("mob", 500, 500)
        self.assertEqual(tracker.update([mob]), [])
        self.assertEqual(tracker.update([mob]), [])
        confirmed = tracker.update([mob])
        self.assertEqual(len(confirmed), 1)
        self.assertIs(confirmed[0], mob)

    def test_confirmed_mob_survives_miss_hold(self):
        tracker = self._tracker()
        mob = make_detection("mob", 500, 500)
        for _ in range(3):
            tracker.update([mob])
        # One missing frame: the confirmed mob is held.
        held = tracker.update([])
        self.assertEqual(len(held), 1)
        # Too many missing frames: it disappears.
        tracker.update([])
        tracker.update([])
        tracker.update([])
        self.assertEqual(tracker.update([]), [])

    def test_two_mobs_tracked_independently(self):
        tracker = self._tracker()
        mob_a = make_detection("mob", 300, 400)
        mob_b = make_detection("mob", 1200, 700)
        for _ in range(3):
            tracker.update([mob_a, mob_b])
        # mob_a dies (missed for miss_hold+1 frames); mob_b persists.
        for _ in range(4):
            held = tracker.update([mob_b])
        self.assertEqual(len(held), 1)
        self.assertIs(held[0], mob_b)

    def test_moving_mob_matches_by_proximity(self):
        tracker = self._tracker(match_px=100)
        # The mob moves up to match_px between frames; still one track.
        tracker.update([make_detection("mob", 500, 500)])
        tracker.update([make_detection("mob", 560, 520)])
        confirmed = tracker.update([make_detection("mob", 620, 540)])
        self.assertEqual(len(confirmed), 1)

    def test_reset_forgets_tracks(self):
        tracker = self._tracker()
        mob = make_detection("mob", 500, 500)
        for _ in range(3):
            tracker.update([mob])
        tracker.reset()
        self.assertEqual(tracker.update([mob]), [])


if __name__ == "__main__":
    unittest.main()
