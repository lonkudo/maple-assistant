import logging
import threading
import unittest
from unittest.mock import patch

from assistant import _AnyEvent, _compact_log_formatter, _start_live_input, parse_args


class FakeSender:
    def __init__(self, foreground: bool = True) -> None:
        self.foreground = foreground
        self.calls = []

    def select_window(self) -> bool:
        self.calls.append("select")
        return True

    def is_game_foreground(self) -> bool:
        self.calls.append("verify")
        return self.foreground

    def enable_input(self) -> None:
        self.calls.append("enable")


class StartLiveInputTests(unittest.TestCase):
    def test_log_formatter_removes_only_worker_suffix(self) -> None:
        formatter = _compact_log_formatter()
        record = logging.LogRecord(
            "movement_worker", logging.INFO, __file__, 1,
            "PATROL| pos=(0.25, 0.52)", (), None,
        )
        record.threadName = "movement-worker"
        rendered = formatter.format(record)
        self.assertIn(" movement INFO PATROL|", rendered)
        self.assertNotIn("movement-worker", rendered)

    def test_attack_worker_is_disabled_by_default(self) -> None:
        with patch("sys.argv", ["assistant.py"]):
            self.assertFalse(parse_args().enable_attack)

    def test_status_capture_is_fast_for_potion_priority(self) -> None:
        with patch("sys.argv", ["assistant.py"]):
            self.assertEqual(parse_args().status_interval, 0.25)

    def test_default_config_path_is_user_owned_file(self) -> None:
        with patch("sys.argv", ["assistant.py"]):
            self.assertEqual(parse_args().config.name, "user_config.json")

    def test_start_selects_and_verifies_game_before_enabling_input(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        _start_live_input(sender, active)
        self.assertEqual(sender.calls, ["select", "verify", "enable"])
        self.assertTrue(active.is_set())

    def test_map_session_is_prepared_before_input_is_enabled(self) -> None:
        sender = FakeSender()
        active = threading.Event()

        def prepare() -> None:
            sender.calls.append("prepare-map")

        _start_live_input(sender, active, prepare)
        self.assertEqual(
            sender.calls, ["select", "verify", "prepare-map", "enable"]
        )
        self.assertTrue(active.is_set())

    def test_capture_calibration_starts_only_after_foreground_verification(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        preparing = threading.Event()

        def prepare() -> None:
            self.assertTrue(preparing.is_set())
            sender.calls.append("stable-minimap")

        _start_live_input(sender, active, prepare, preparing)
        self.assertEqual(
            sender.calls,
            ["select", "verify", "stable-minimap", "enable"],
        )
        self.assertFalse(preparing.is_set())
        self.assertTrue(active.is_set())

    def test_capture_calibration_gate_clears_when_detection_fails(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        preparing = threading.Event()

        with self.assertRaisesRegex(OSError, "unstable minimap"):
            _start_live_input(
                sender, active,
                lambda: (_ for _ in ()).throw(OSError("unstable minimap")),
                preparing,
            )
        self.assertFalse(preparing.is_set())
        self.assertFalse(active.is_set())
        self.assertEqual(sender.calls, ["select", "verify"])

    def test_combined_capture_gate_accepts_focus_or_preparation(self) -> None:
        focused = threading.Event()
        preparing = threading.Event()
        gate = _AnyEvent(focused, preparing)
        self.assertFalse(gate.is_set())
        preparing.set()
        self.assertTrue(gate.is_set())
        preparing.clear()
        focused.set()
        self.assertTrue(gate.is_set())

    def test_failed_map_match_does_not_enable_input(self) -> None:
        sender = FakeSender()
        active = threading.Event()

        def reject() -> None:
            raise OSError("wrong map")

        with self.assertRaisesRegex(OSError, "wrong map"):
            _start_live_input(sender, active, reject)
        self.assertEqual(sender.calls, ["select", "verify"])
        self.assertFalse(active.is_set())

    def test_failed_foreground_verification_does_not_enable_input(self) -> None:
        sender = FakeSender(foreground=False)
        active = threading.Event()
        with self.assertRaises(OSError):
            _start_live_input(sender, active)
        self.assertEqual(sender.calls, ["select", "verify"])
        self.assertFalse(active.is_set())


if __name__ == "__main__":
    unittest.main()
