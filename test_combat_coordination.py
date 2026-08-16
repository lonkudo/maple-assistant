"""Tests for the cross-process attack/patrol coordination state file."""

import tempfile
import time
import unittest
from pathlib import Path

from combat_coordination import AttackStateFile


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


if __name__ == "__main__":
    unittest.main()
