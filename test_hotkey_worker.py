import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest

from hotkey_worker import HotkeyWorker, KEY_VK


class HotkeyWorkerTests(unittest.TestCase):
    def test_loads_physical_ctrl_bindings_and_block_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotkey.json"
            path.write_text(json.dumps({
                "enabled": True,
                "ignore_injected": True,
                "bindings": [
                    {"keys": "ctrl+1", "action": "quick_message:0",
                     "block_original": True},
                    {"keys": "ctrl+left", "action": "record:left_most_pos",
                     "block_original": False},
                    {"keys": "alt+1", "action": "ignored"},
                ],
            }), encoding="utf-8")
            worker = HotkeyWorker(
                threading.Event(), queue.Queue(), config_path=path
            )
            self.assertTrue(worker.enabled)
            self.assertTrue(worker.ignore_injected)
            self.assertEqual(
                worker._bindings[KEY_VK["1"]], ("quick_message:0", True)
            )
            self.assertEqual(
                worker._bindings[KEY_VK["left"]],
                ("record:left_most_pos", False),
            )
            self.assertEqual(len(worker._bindings), 2)

    def test_default_config_maps_ten_messages_in_insertion_order(self) -> None:
        config = Path(__file__).with_name("hotkey.json")
        worker = HotkeyWorker(threading.Event(), queue.Queue(), config_path=config)
        digits = list("1234567890")
        actions = [worker._bindings[KEY_VK[key]][0] for key in digits]
        self.assertEqual(actions, [f"quick_message:{i}" for i in range(10)])
        self.assertEqual(
            worker._bindings[KEY_VK["left"]][0], "record:left_most_pos"
        )
        self.assertEqual(
            worker._bindings[KEY_VK["right"]][0], "record:right_most_pos"
        )
        self.assertEqual(
            worker._bindings[KEY_VK["grave"]][0], "toggle_patrol"
        )

    def test_patrol_running_allows_only_toggle_patrol(self) -> None:
        config = Path(__file__).with_name("hotkey.json")
        worker = HotkeyWorker(threading.Event(), queue.Queue(), config_path=config)
        # Not patrolling: every binding is allowed.
        self.assertTrue(worker._binding_allowed("quick_message:0"))
        self.assertTrue(worker._binding_allowed("record:left_most_pos"))
        self.assertTrue(worker._binding_allowed("toggle_patrol"))

        worker.set_patrol_running(True)
        # While patrol runs only the patrol-toggle chord and the fixed-attack
        # interval adjustment stay live.
        self.assertFalse(worker._binding_allowed("quick_message:0"))
        self.assertFalse(worker._binding_allowed("record:left_most_pos"))
        self.assertTrue(
            worker._binding_allowed("adjust_fixed_attack_interval:+0.1")
        )
        self.assertTrue(
            worker._binding_allowed("adjust_fixed_attack_interval:-0.1")
        )
        self.assertTrue(worker._binding_allowed("toggle_patrol"))

        worker.set_patrol_running(False)
        self.assertTrue(worker._binding_allowed("quick_message:0"))


    def test_ignore_injected_drops_only_self_and_lower_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotkey.json"
            path.write_text(json.dumps({
                "enabled": True,
                "ignore_injected": True,
                "bindings": [],
            }), encoding="utf-8")
            worker = HotkeyWorker(
                threading.Event(), queue.Queue(), config_path=path
            )
            # Physical events always pass.
            self.assertFalse(worker._should_ignore_injected(0, 0))
            # Foreign same-integrity injection (Mouse Without Borders etc.):
            # LLKHF_INJECTED only, no self marker -> passes like physical.
            self.assertFalse(worker._should_ignore_injected(0x10, 0))
            # The assistant's own stamped SendInput events are ignored.
            self.assertTrue(worker._should_ignore_injected(0x10, 0x4D4150))
            # Lower-integrity injection is always ignored (spoofing vector).
            self.assertTrue(worker._should_ignore_injected(0x12, 0))
            self.assertTrue(worker._should_ignore_injected(0x12, 0x4D4150))

    def test_ignore_injected_false_accepts_every_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotkey.json"
            path.write_text(json.dumps({
                "enabled": True,
                "ignore_injected": False,
                "bindings": [],
            }), encoding="utf-8")
            worker = HotkeyWorker(
                threading.Event(), queue.Queue(), config_path=path
            )
            for flags, extra_info in (
                (0, 0),
                (0x10, 0),
                (0x10, 0x4D4150),
                (0x12, 0x4D4150),
            ):
                self.assertFalse(
                    worker._should_ignore_injected(flags, extra_info),
                    f"flags=0x{flags:x} extra_info=0x{extra_info:x} ignored",
                )


if __name__ == "__main__":
    unittest.main()
