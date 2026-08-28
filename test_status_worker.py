import queue
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from PIL import Image, ImageDraw

from capture_worker import remap_normalized_box
from status_worker import (
    BarStatusDetector, StatusConfig, StatusReading, StatusWorker,
    WindowKeySender, apply_drug_settings,
)


def status_image(hp_ratio: float, mp_ratio: float) -> Image.Image:
    image = Image.new("RGB", (1000, 1000), "black")
    draw = ImageDraw.Draw(image)
    # Detector expects a 51 px full bar at this resolution (0.0512 of width).
    draw.rectangle((400, 970, 400 + round(51 * hp_ratio) - 1, 974),
                   fill=(220, 20, 20))
    draw.rectangle((400, 985, 400 + round(51 * mp_ratio) - 1, 989),
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

    def test_adaptive_full_bar_reference_handles_fixed_pixel_hud(self) -> None:
        # Fixed-pixel HUD: the bars are 100px wide on a 1000px frame while the
        # client-fraction estimate says 51px.  Once the full bar is observed
        # the reference adapts and ratios are correct - previously every ratio
        # clipped to 1.0 and potions never fired on such machines.
        def frame(hp_px: int, mp_px: int) -> Image.Image:
            image = Image.new("RGB", (1000, 1000), "black")
            draw = ImageDraw.Draw(image)
            draw.rectangle((400, 970, 400 + hp_px - 1, 974),
                           fill=(220, 20, 20))
            draw.rectangle((400, 985, 400 + mp_px - 1, 989),
                           fill=(20, 40, 220))
            return image

        detector = BarStatusDetector()
        first = detector.detect(frame(100, 100))  # both full: adapts refs
        self.assertAlmostEqual(first.hp, 656, delta=5)
        self.assertAlmostEqual(first.mp, 371, delta=5)
        half = detector.detect(frame(50, 100))    # HP at 50% of the real bar
        self.assertAlmostEqual(half.hp, 328, delta=10)
        self.assertAlmostEqual(half.mp, 371, delta=5)

    def test_wide_non_bar_element_does_not_lock_ratio_at_full(self):
        # A wide blue element (HUD frame / bar-track glow) inside the ROI
        # must NOT be measured as the MP bar - it would lock the ratio at
        # 1.0 and MP potions would never fire.  The real fill is used.
        image = Image.new("RGB", (1000, 1000), "black")
        draw = ImageDraw.Draw(image)
        # Wide blue artifact, passes the MP mask, sits in its own row band.
        draw.rectangle((340, 975, 639, 979), fill=(60, 120, 220))
        # Real MP fill at roughly half length (25px of the 51px estimate).
        draw.rectangle((400, 985, 424, 989), fill=(20, 40, 220))
        reading = BarStatusDetector().detect(image)
        self.assertIsNotNone(reading.mp_ratio)
        self.assertLess(reading.mp_ratio, 0.7)
        self.assertGreater(reading.mp_ratio, 0.3)

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

    def test_critical_low_ratio_eats_even_at_low_confidence(self) -> None:
        # A near-empty bar is a tiny fill run, which reads with LOW
        # confidence exactly when the potion is needed.  Potions are the
        # highest priority: a low-confidence read with a bar below its
        # threshold must still attempt the potion.
        class FakeDetector:
            def __init__(self, reading, config):
                self.reading = reading
                self.config = config

            def detect(self, image):
                return self.reading

        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event(),
                              potion_cooldown=60, low_frames_required=1)
        config = replace(
            worker.detector.config,
            hp_ratio_threshold=0.5, mp_ratio_threshold=0.3,
        )
        reading = StatusReading(
            hp=10, mp=3, hp_ratio=0.02, mp_ratio=0.01, confidence=0.20
        )
        worker.detector = FakeDetector(reading, config)
        worker._process_frame(status_image(0.5, 0.2))
        self.assertEqual(sender.keys, ["delete", "end"])

    def test_blocked_potion_tap_is_retried(self) -> None:
        # A transiently blocked potion tap (foreground flicker, momentary key
        # ownership) must be retried instead of leaving the character unable
        # to eat.
        class FlakySender(FakeSender):
            def __init__(self):
                super().__init__()
                self.fail_until = 1

            def tap(self, key: str) -> bool:
                if self.fail_until > 0:
                    self.fail_until -= 1
                    return False
                self.keys.append(key)
                return True

        sender = FlakySender()
        worker = StatusWorker(
            queue.Queue(), sender, threading.Event(),
            potion_cooldown=60, low_frames_required=1,
            potion_retry_attempts=3, potion_retry_delay_seconds=0.0,
        )
        worker._process_frame(status_image(0.4, 0.1))
        self.assertEqual(sender.keys, ["delete", "end"])

    def test_apply_drug_settings_maps_percent_to_ratio_and_validates_keys(self) -> None:
        config = StatusConfig()
        updated = apply_drug_settings(config, {
            "hp_key": "1", "mp_key": "space",
            "hp_threshold": 55, "mp_threshold": 20,
            "hp_enabled": False, "mp_enabled": True,
        })
        self.assertEqual(updated.hp_key, "1")
        self.assertEqual(updated.mp_key, "space")
        self.assertAlmostEqual(updated.hp_ratio_threshold, 0.55)
        self.assertAlmostEqual(updated.mp_ratio_threshold, 0.20)
        self.assertFalse(updated.hp_enabled)
        self.assertTrue(updated.mp_enabled)
        # Keys outside the bindable whitelist are ignored: the existing
        # binding stays.
        unchanged = apply_drug_settings(config, {"hp_key": "q"})
        self.assertEqual(unchanged.hp_key, config.hp_key)

    def test_apply_drug_settings_maps_buff_keys_intervals_and_enabled(self) -> None:
        config = StatusConfig()
        updated = apply_drug_settings(config, {
            "buff1_key": "home", "buff2_key": "space",
            "buff1_interval": 10.0, "buff2_interval": 5.5,
            "buff1_enabled": True, "buff2_enabled": True,
        })
        self.assertEqual(updated.buff1_key, "home")
        self.assertEqual(updated.buff2_key, "space")
        # Minutes in the UI form become seconds in the worker config.
        self.assertAlmostEqual(updated.buff1_interval, 600.0)
        self.assertAlmostEqual(updated.buff2_interval, 330.0)
        self.assertTrue(updated.buff1_enabled)
        self.assertTrue(updated.buff2_enabled)
        # Unbindable key and malformed interval are ignored: defaults stay.
        ignored = apply_drug_settings(config, {
            "buff1_key": "q", "buff2_interval": "oops",
        })
        self.assertEqual(ignored.buff1_key, config.buff1_key)
        self.assertEqual(ignored.buff2_interval, config.buff2_interval)

    def test_periodic_buff_taps_on_interval_timer(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event())
        worker.detector.config = replace(
            worker.detector.config,
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
        )
        # 增益不从开局立即触发（用户会先手动触发第一次）：启动后第一帧不按。
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, [])
        # 计时器到期后按一次（回拨时间戳模拟时间流逝）。
        worker._last_buff["buff1"] = time.monotonic() - 61.0
        worker._last_buff["buff2"] = time.monotonic() - 61.0
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, ["home", "insert"])
        # Interval not elapsed yet: no repeat.
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, ["home", "insert"])
        # Backdate both timers: next frame refreshes both buffs again.
        worker._last_buff["buff1"] = time.monotonic() - 61.0
        worker._last_buff["buff2"] = time.monotonic() - 61.0
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, ["home", "insert", "home", "insert"])

    def test_disabled_or_unbound_buff_never_taps(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event())
        # Defaults: both buff rows disabled -> nothing fires.
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, [])
        # Enabled but empty keys still never fire.
        worker.detector.config = replace(
            worker.detector.config,
            buff1_key="", buff2_key="",
            buff1_enabled=True, buff2_enabled=True,
        )
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, [])

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

    def test_status_state_file_publishes_hp_ratio(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        sender = FakeSender()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "status_state.json"
            worker = StatusWorker(
                queue.Queue(), sender, threading.Event(),
                potion_cooldown=60, low_frames_required=2,
                status_state_path=str(state_path),
            )
            image = status_image(0.4, 0.1)
            worker._process_frame(image)
            self.assertTrue(state_path.is_file())
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(data["hp_ratio"], 0.4, places=1)
            self.assertAlmostEqual(data["mp_ratio"], 0.1, places=1)

    def test_status_state_file_skipped_when_not_configured(self) -> None:
        import tempfile
        from pathlib import Path

        sender = FakeSender()
        with tempfile.TemporaryDirectory() as directory:
            worker = StatusWorker(queue.Queue(), sender, threading.Event())
            image = status_image(0.4, 0.1)
            worker._process_frame(image)
            self.assertEqual(
                list(Path(directory).glob("status_state.json")), []
            )


if __name__ == "__main__":
    unittest.main()
