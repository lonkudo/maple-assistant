import tempfile
import unittest
from pathlib import Path

from timer_state import load_timer_state, save_timer_state


class TimerStateTests(unittest.TestCase):
    def test_round_trips_enabled_deadline_and_interval(self):
        path = Path(tempfile.mkdtemp()) / "timer.json"
        save_timer_state(
            path, enabled=True, deadline_at=1_700_000_000.25,
            interval_seconds=3600.0,
        )
        self.assertEqual(load_timer_state(path), {
            "enabled": True,
            "deadline_at": 1_700_000_000.25,
            "interval_seconds": 3600.0,
        })

    def test_missing_or_bad_state_is_empty(self):
        path = Path(tempfile.mkdtemp()) / "timer.json"
        self.assertEqual(load_timer_state(path), {})
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(load_timer_state(path), {})


if __name__ == "__main__":
    unittest.main()
