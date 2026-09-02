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
        # While patrol runs only the patrol-toggle chord stays live.
        self.assertFalse(worker._binding_allowed("quick_message:0"))
        self.assertFalse(worker._binding_allowed("record:left_most_pos"))
        self.assertFalse(worker._binding_allowed("adjust_fixed_attack_interval:+0.1"))
        self.assertTrue(worker._binding_allowed("toggle_patrol"))

        worker.set_patrol_running(False)
        self.assertTrue(worker._binding_allowed("quick_message:0"))


if __name__ == "__main__":
    unittest.main()
