import threading
import time
import unittest

from attack_worker import AttackWorker


class FakeSender:
    def __init__(self):
        self.events = []

    def key_down(self, key):
        self.events.append(("down", key))
        return True

    def key_up(self, key):
        self.events.append(("up", key))
        return True


class AttackWorkerTests(unittest.TestCase):
    def test_attack_once_is_only_ctrl_down_up(self):
        sender = FakeSender()
        worker = AttackWorker(sender, threading.Event())
        self.assertTrue(worker.attack_once())
        self.assertEqual(sender.events, [("down", "ctrl"), ("up", "ctrl")])

    def test_attack_once_uses_configured_key(self):
        sender = FakeSender()
        worker = AttackWorker(sender, threading.Event(), attack_key="shift")
        self.assertTrue(worker.attack_once())
        self.assertEqual(sender.events, [("down", "shift"), ("up", "shift")])

    def test_set_key_validates_against_sender_scan_map(self):
        class ScannedSender(FakeSender):
            _SCAN = {"ctrl": (0x1D, False), "shift": (0x2A, False)}

        worker = AttackWorker(ScannedSender(), threading.Event())
        self.assertTrue(worker.set_key("Shift"))
        self.assertEqual(worker.attack_key, "shift")
        self.assertFalse(worker.set_key("home"))  # not in the fake scan map
        self.assertEqual(worker.attack_key, "shift")

    def test_unsupported_constructor_key_falls_back_to_ctrl(self):
        class ScannedSender(FakeSender):
            _SCAN = {"ctrl": (0x1D, False)}

        worker = AttackWorker(ScannedSender(), threading.Event(),
                              attack_key="nope")
        self.assertEqual(worker.attack_key, "ctrl")

    def test_disabled_worker_never_attacks(self):
        sender = FakeSender()
        stop = threading.Event()
        worker = AttackWorker(sender, stop, .25, initial_offset=.25)
        worker.enabled = False
        worker.start()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertEqual(sender.events, [])

    def test_timer_runs_without_frames(self):
        sender = FakeSender()
        stop = threading.Event()
        worker = AttackWorker(sender, stop, .25, initial_offset=.25)
        worker.start()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertEqual(sender.events[:2], [("down", "ctrl"), ("up", "ctrl")])

    def test_climbing_blocks_attack_then_attack_resumes(self):
        sender = FakeSender()
        stop = threading.Event()
        climbing = threading.Event()
        climbing.set()
        worker = AttackWorker(sender, stop, .25,
                              climbing_active_event=climbing,
                              initial_offset=.25)
        worker.start()
        time.sleep(.30)
        self.assertEqual(sender.events, [])
        climbing.clear()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertGreaterEqual(len(sender.events), 2)
        self.assertEqual(sender.events[:2], [("down", "ctrl"), ("up", "ctrl")])

    def test_default_attack_clock_is_half_interval_offset(self):
        worker = AttackWorker(FakeSender(), threading.Event(), 3.0)
        self.assertEqual(worker.initial_offset, 1.5)

    def test_attack_publishes_attack_state_window(self):
        # 固定攻击攻击时发布"激活"窗口（移动线程据此暂停行走）。
        import tempfile
        from pathlib import Path
        from combat_coordination import AttackStateFile

        sender = FakeSender()
        stop = threading.Event()
        tmp = Path(tempfile.mkdtemp()) / "attack_state.json"
        state = AttackStateFile(str(tmp))
        worker = AttackWorker(sender, stop, .25,
                              initial_offset=.25,
                              attack_state_path=str(tmp),
                              attack_pause_seconds=0.2)
        worker.start()
        time.sleep(.30)
        stop.set()
        worker.join(1)
        self.assertEqual(sender.events[:2], [("down", "ctrl"), ("up", "ctrl")])
        # 攻击结束后状态已清除。
        self.assertFalse(state.is_active())
        # 攻击期间状态曾经为激活（通过事件序列验证键已发送，且窗口写入过）。
        self.assertTrue(tmp.exists())


if __name__ == "__main__":
    unittest.main()
