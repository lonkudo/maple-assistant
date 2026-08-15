import threading
import unittest

from assistant import _start_live_input


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
    def test_start_selects_and_verifies_game_before_enabling_input(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        _start_live_input(sender, active)
        self.assertEqual(sender.calls, ["select", "verify", "enable"])
        self.assertTrue(active.is_set())

    def test_failed_foreground_verification_does_not_enable_input(self) -> None:
        sender = FakeSender(foreground=False)
        active = threading.Event()
        with self.assertRaises(OSError):
            _start_live_input(sender, active)
        self.assertEqual(sender.calls, ["select", "verify"])
        self.assertFalse(active.is_set())


if __name__ == "__main__":
    unittest.main()
