import queue
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from PIL import Image, ImageDraw

from capture_worker import remap_normalized_box
from status_worker import (
    BarStatusDetector, StatusConfig, StatusWorker, WindowKeySender,
    apply_drug_settings,
)


def status_image(hp_ratio: float, mp_ratio: float) -> Image.Image:
    image = Image.new("RGB", (1000, 1000), "black")
    draw = ImageDraw.Draw(image)
    # Detector expects a 77 px full bar at this resolution.
    draw.rectangle((400, 970, 400 + round(77 * hp_ratio) - 1, 974),
                   fill=(220, 20, 20))
    draw.rectangle((400, 985, 400 + round(77 * mp_ratio) - 1, 989),
                   fill=(20, 40, 220))
    return image


class FakeSender:
    def __init__(self) -> None:
        self.keys = []

    def tap(self, key: str) -> bool:
        self.keys.append(key)
        return True


class StatusTests(unittest.TestCase):
    def test_bar_ratios_are_converted_to_values(self) -> None:
        reading = BarStatusDetector().detect(status_image(0.5, 0.2))
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)

    def test_bar_calibration_survives_left_sixty_percent_capture_crop(self) -> None:
        full = status_image(0.5, 0.2)
        cropped = full.crop((0, 0, 600, 1000))
        defaults = StatusConfig()
        config = replace(
            defaults,
            status_roi=remap_normalized_box(
                defaults.status_roi, (0.0, 0.0, 0.60, 1.0)
            ),
            full_bar_width_fraction=defaults.full_bar_width_fraction / 0.60,
            min_bar_width_fraction=defaults.min_bar_width_fraction / 0.60,
        )
        reading = BarStatusDetector(config).detect(cropped)
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)

    def test_bar_calibration_uses_status_only_capture(self) -> None:
        full = status_image(0.5, 0.2)
        cropped = full.crop((340, 960, 560, 1000))
        defaults = StatusConfig()
        config = replace(
            defaults,
            status_roi=(0.0, 0.0, 1.0, 1.0),
            full_bar_width_fraction=defaults.full_bar_width_fraction / 0.22,
            min_bar_width_fraction=defaults.min_bar_width_fraction / 0.22,
        )
        reading = BarStatusDetector(config).detect(cropped)
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)

    def test_two_low_frames_trigger_once_and_cooldown_debounces(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event(),
                              potion_cooldown=60, low_frames_required=2)
        image = status_image(0.4, 0.1)
        worker._process_frame(image)
        self.assertEqual(sender.keys, [])
        worker._process_frame(image)
        self.assertEqual(sender.keys, ["delete", "end"])
        worker._process_frame(image)
        worker._process_frame(image)
        self.assertEqual(sender.keys, ["delete", "end"])

    def test_drug_uses_configured_keys_and_percent_thresholds(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event(),
                              potion_cooldown=60, low_frames_required=2)
        worker.detector.config = replace(
            worker.detector.config,
            hp_key="1", mp_key="2",
            hp_ratio_threshold=0.6, mp_ratio_threshold=0.2,
        )
        image = status_image(0.4, 0.1)  # 40% < 60%, 10% < 20%
        worker._process_frame(image)
        worker._process_frame(image)
        self.assertEqual(sender.keys, ["1", "2"])

    def test_disabled_drug_never_taps(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event(),
                              potion_cooldown=60, low_frames_required=1)
        worker.detector.config = replace(
            worker.detector.config,
            hp_enabled=False, mp_enabled=True, mp_ratio_threshold=0.5,
        )
        worker._process_frame(status_image(0.1, 0.1))
        self.assertEqual(sender.keys, ["end"])

    def test_apply_drug_settings_maps_percent_to_ratio_and_validates_keys(self) -> None:
        config = StatusConfig()
        updated = apply_drug_settings(config, {
            "hp_key": "1", "mp_key": "f4",
            "hp_threshold": 55, "mp_threshold": 20,
            "hp_enabled": False, "mp_enabled": True,
        })
        self.assertEqual(updated.hp_key, "1")
        self.assertEqual(updated.mp_key, "f4")
        self.assertAlmostEqual(updated.hp_ratio_threshold, 0.55)
        self.assertAlmostEqual(updated.mp_ratio_threshold, 0.20)
        self.assertFalse(updated.hp_enabled)
        self.assertTrue(updated.mp_enabled)
        # Unsupported key is ignored: the existing binding stays.
        unchanged = apply_drug_settings(config, {"hp_key": "not-a-key"})
        self.assertEqual(unchanged.hp_key, config.hp_key)

    def test_new_scan_codes_cover_potion_keys(self) -> None:
        sender = WindowKeySender("game")
        for key in ("1", "9", "q", "m", "f1", "f12", "end",
                    "shift", "tab", "enter", "kp_7", "kp_add",
                    "minus", "pageup"):
            self.assertIn(key, sender._SCAN)

    def test_sender_is_dry_run_by_default(self) -> None:
        sender = WindowKeySender("game")
        self.assertTrue(sender.dry_run)
        self.assertTrue(sender.tap("ctrl"))

    def test_disabled_input_does_not_select_window_or_send_keys(self) -> None:
        sender = WindowKeySender("game", dry_run=False, input_enabled=False)
        selections = []
        events = []
        sender.select_window = lambda: selections.append(True)
        sender._send_scan_code = lambda code, key_up, extended: events.append(key_up)

        self.assertFalse(sender.tap("ctrl"))
        self.assertEqual(selections, [])
        self.assertEqual(events, [])

        sender.enable_input()
        sender._foreground_matches = lambda: True
        self.assertTrue(sender.tap("ctrl"))
        self.assertEqual(events, [False, True])

    def test_focus_loss_blocks_keys_without_reselecting_window(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        selections = []
        sender._foreground_matches = lambda: False
        sender.select_window = lambda: selections.append(True)
        sender._send_scan_code = lambda *args, **kwargs: self.fail(
            "no input should be injected while unfocused"
        )

        self.assertFalse(sender.is_target_focused())
        self.assertFalse(sender.tap("ctrl"))
        self.assertEqual(selections, [])

    def test_two_second_hold_is_not_clamped(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(
            (code, key_up, extended)
        )
        with patch("status_worker.time.sleep") as sleep:
            self.assertTrue(sender.press("left", duration=2.0))
        sleep.assert_called_once_with(2.0)
        self.assertEqual([event[1] for event in events], [False, True])

    def test_direction_repeat_does_not_add_key_owners(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(key_up)
        self.assertTrue(sender.key_down("right"))
        self.assertTrue(sender.repeat_key_down("right"))
        self.assertTrue(sender.repeat_key_down("right"))
        self.assertTrue(sender.key_up("right"))
        self.assertFalse(sender.is_key_down("right"))
        self.assertEqual(events, [False, False, False, True])

    def test_second_owner_cannot_release_movement_hold(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(key_up)
        self.assertTrue(sender.key_down("left"))       # movement owns Left
        self.assertTrue(sender.key_down("left"))       # attack also owns Left
        self.assertTrue(sender.key_up("left"))         # attack releases only itself
        self.assertEqual(events, [False])               # no physical key-up yet
        self.assertTrue(sender.key_up("left"))         # movement finishes
        self.assertEqual(events, [False, True])



if __name__ == "__main__":
    unittest.main()
