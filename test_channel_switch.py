import unittest
from unittest import mock

from channel_switch import channel_switch_procedure


class FakeSender:
    def __init__(self):
        self.pressed = []

    def press(self, key, duration=0.025):
        self.pressed.append(key)
        return True


class BlockingSender(FakeSender):
    def press(self, key, duration=0.025):
        self.pressed.append(key)
        return False  # blocked immediately


class ChannelSwitchTests(unittest.TestCase):
    def test_fixed_sequence_with_overridden_counts(self):
        sender = FakeSender()
        with mock.patch("channel_switch.time.sleep") as sleep:
            ok = channel_switch_procedure(
                sender, left_count=3, down_count=8,
                key_delay=0.2, hold=0.06, wait=3.0,
            )
        self.assertTrue(ok)
        self.assertEqual(sender.pressed, [
            "esc", "enter",
            "left", "left", "left",
            "down", "down", "down", "down", "down", "down", "down", "down",
            "enter",
        ])
        # One sleep per key (14) + the final 3s wait.
        self.assertEqual(sleep.call_count, 15)

    def test_random_counts_stay_within_1_to_10(self):
        sender = FakeSender()
        with mock.patch("channel_switch.time.sleep"):
            ok = channel_switch_procedure(sender)
        self.assertTrue(ok)
        lefts = [k for k in sender.pressed if k == "left"]
        downs = [k for k in sender.pressed if k == "down"]
        self.assertTrue(1 <= len(lefts) <= 10)
        self.assertTrue(1 <= len(downs) <= 10)
        # Structure: esc, enter, lefts, downs, enter.
        self.assertEqual(sender.pressed[0], "esc")
        self.assertEqual(sender.pressed[1], "enter")
        self.assertEqual(sender.pressed[-1], "enter")

    def test_on_press_callback_receives_every_key(self):
        sender = FakeSender()
        seen = []
        with mock.patch("channel_switch.time.sleep"):
            channel_switch_procedure(
                sender, left_count=1, down_count=1,
                on_press=lambda key, ok: seen.append((key, ok)),
            )
        self.assertEqual([k for k, _ in seen], sender.pressed)
        self.assertTrue(all(ok for _, ok in seen))

    def test_blocked_key_aborts_and_reports_false(self):
        sender = BlockingSender()
        with mock.patch("channel_switch.time.sleep"):
            ok = channel_switch_procedure(sender, left_count=2, down_count=2)
        self.assertFalse(ok)
        self.assertEqual(sender.pressed[:1], ["esc"])  # stopped at the first key


if __name__ == "__main__":
    unittest.main()
