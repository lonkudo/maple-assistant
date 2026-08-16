"""Tests for the cross-process attack/patrol coordination state file."""

import tempfile
import time
import unittest
from pathlib import Path

from combat_coordination import (
    AttackStateFile,
    PatrolStateFile,
    RopeStateFile,
)


class AttackStateFileTests(unittest.TestCase):
    def _path(self) -> Path:
        return Path(tempfile.mkdtemp()) / "attack_state.json"

    def test_write_then_active(self):
        path = self._path()
        state = AttackStateFile(str(path))
        state.write(True, (123.0, 456.0))
        self.assertTrue(state.is_active())
        self.assertEqual(state.target(), (123.0, 456.0))

    def test_write_inactive(self):
        path = self._path()
        state = AttackStateFile(str(path))
        state.write(False)
        self.assertFalse(state.is_active())
        self.assertIsNone(state.target())

    def test_missing_file_is_inactive(self):
        state = AttackStateFile(str(self._path()))
        self.assertFalse(state.is_active())
        self.assertIsNone(state.target())

    def test_stale_file_is_inactive(self):
        path = self._path()
        state = AttackStateFile(str(path))
        state.write(True, (1.0, 2.0))
        # Simulate an old timestamp (e.g. the YOLO subprocess died).
        data = state.read()
        data["ts"] = time.time() - 60.0
        path.write_text(__import__("json").dumps(data), encoding="utf-8")
        self.assertFalse(state.is_active(max_age=1.0))

    def test_fresh_file_is_active_within_max_age(self):
        path = self._path()
        state = AttackStateFile(str(path))
        state.write(True, (1.0, 2.0))
        # max_age of 0 would reject anything older than now; 5s accepts it.
        self.assertTrue(state.is_active(max_age=5.0))

    def test_corrupt_file_is_inactive(self):
        path = self._path()
        path.write_text("{ not json", encoding="utf-8")
        state = AttackStateFile(str(path))
        self.assertFalse(state.is_active())
        self.assertIsNone(state.target())


class RopeStateFileTests(unittest.TestCase):
    def _path(self):
        import tempfile
        from pathlib import Path

        return Path(tempfile.mkdtemp()) / "rope_state.json"

    def test_write_and_screen_gap(self):
        path = self._path()
        state = RopeStateFile(str(path))
        state.write(True, rope_x=1400.0, char_x=1280.0)
        self.assertTrue(state.is_fresh())
        self.assertAlmostEqual(state.screen_gap(), 120.0)

    def test_invisible_has_no_gap(self):
        path = self._path()
        state = RopeStateFile(str(path))
        state.write(False)
        self.assertTrue(state.is_fresh())
        self.assertIsNone(state.screen_gap())

    def test_missing_file_is_not_fresh(self):
        state = RopeStateFile(str(self._path()))
        self.assertFalse(state.is_fresh())
        self.assertIsNone(state.screen_gap())

    def test_stale_file_is_not_fresh(self):
        path = self._path()
        state = RopeStateFile(str(path))
        state.write(True, rope_x=1400.0, char_x=1280.0)
        data = state.read()
        data["ts"] = time.time() - 60.0
        path.write_text(__import__("json").dumps(data), encoding="utf-8")
        self.assertFalse(state.is_fresh(max_age=1.0))


class PatrolStateFileTests(unittest.TestCase):
    def _path(self):
        import tempfile
        from pathlib import Path

        return Path(tempfile.mkdtemp()) / "patrol_state.json"

    def test_busy_when_climbing(self):
        path = self._path()
        state = PatrolStateFile(str(path))
        state.write(True, "climb")
        self.assertTrue(state.is_busy())

    def test_idle_not_busy(self):
        path = self._path()
        state = PatrolStateFile(str(path))
        state.write(False)
        self.assertFalse(state.is_busy())

    def test_missing_file_not_busy(self):
        state = PatrolStateFile(str(self._path()))
        self.assertFalse(state.is_busy())

    def test_stale_busy_is_ignored(self):
        path = self._path()
        state = PatrolStateFile(str(path))
        state.write(True, "climb")
        data = state.read()
        data["ts"] = time.time() - 60.0
        path.write_text(__import__("json").dumps(data), encoding="utf-8")
        self.assertFalse(state.is_busy(max_age=1.0))


if __name__ == "__main__":
    unittest.main()
