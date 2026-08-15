import threading
import unittest
from unittest.mock import patch

from assistant import _start_live_input, parse_args


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
    def test_attack_worker_is_disabled_by_default(self) -> None:
        with patch("sys.argv", ["assistant.py"]):
            self.assertFalse(parse_args().enable_attack)

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
