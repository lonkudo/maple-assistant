import queue
import sys
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


# Measured on the current real client (new UI; reference capture 538x40):
# every bar is ~130 px wide at the 1080x768 preset, i.e. 164 px inside this
# capture.  HP red, MP blue and EXP yellow sit side by side in one vertical
# band (rows 20-38).
CAP_W, CAP_H = 538, 40
FULL_W = 164
HP_BAR = (11, 11 + FULL_W - 1)
MP_BAR = (183, 183 + FULL_W - 1)
EXP_BAR = (360, 360 + FULL_W - 1)
BAND_TOP, BAND_BOTTOM = 20, 38


def status_image(hp_ratio: float, mp_ratio: float,
                 exp_ratio: float = 1.0) -> Image.Image:
    image = Image.new("RGB", (CAP_W, CAP_H), "black")
    draw = ImageDraw.Draw(image)
    # Gray tracks behind the three bars.
    for left, right in (HP_BAR, MP_BAR, EXP_BAR):
        draw.rectangle((left, BAND_TOP, right, BAND_BOTTOM),
                       fill=(204, 204, 204))
    # Fills: HP red, MP blue, EXP yellow.
    hp_width = round(FULL_W * hp_ratio)
    draw.rectangle((HP_BAR[0], BAND_TOP, HP_BAR[0] + hp_width, BAND_BOTTOM),
                   fill=(220, 20, 20))
    mp_width = round(FULL_W * mp_ratio)
    draw.rectangle((MP_BAR[0], BAND_TOP, MP_BAR[0] + mp_width, BAND_BOTTOM),
                   fill=(20, 40, 220))
    exp_width = round(FULL_W * exp_ratio)
    draw.rectangle((EXP_BAR[0], BAND_TOP, EXP_BAR[0] + exp_width, BAND_BOTTOM),
                   fill=(238, 255, 0))
    return image


class FakeSender:
    def __init__(self) -> None:
        self.keys = []

    def tap(self, key: str) -> bool:
        self.keys.append(key)
        return True


class DeferredBuffArbiter:
    """Records a buff request and completes it only when the test says so."""

    def __init__(self) -> None:
        self.requests = []

    def request_buff(self, key, on_complete=None) -> bool:
        self.requests.append((key, on_complete))
        return True


class StatusTests(unittest.TestCase):
    def test_disable_input_refocuses_before_resetting_held_keys(self) -> None:
        sender = WindowKeySender("MapleStory", dry_run=False)
        calls = []
        sender.select_window = lambda: calls.append("select") or True
        sender.reset_input_session = lambda reason: calls.append(("reset", reason)) or 1

        sender.disable_input(refocus_before_release=True)

        self.assertEqual(calls, ["select", ("reset", "input disabled")])
        self.assertFalse(sender.input_is_enabled())

    def test_exact_window_title_lookup_avoids_fallback_enumeration(self) -> None:
        class FakeWin32Gui:
            @staticmethod
            def FindWindow(_class, title):
                return 42 if title == "MapleStory" else 0

            @staticmethod
            def IsWindowVisible(hwnd):
                return hwnd == 42

            @staticmethod
            def EnumWindows(_callback, _extra):
                raise AssertionError("fallback enumeration should not run")

        sender = WindowKeySender("MapleStory", dry_run=True)
        with patch.dict(sys.modules, {"win32gui": FakeWin32Gui}):
            self.assertEqual(sender._find_target_window(), 42)

    def test_the_presence_probe_never_raises_without_the_game(self) -> None:
        # The quick pickup hotkey asks whether the game is there at all - while the
        # operator tests a 测试测谎 video it is not, and that must not become an error.
        # No keyword guessing either: a window titled "MapleStory wiki" in a browser
        # must never be mistaken for the game.
        class FakeWin32Gui:
            @staticmethod
            def FindWindow(_class, _title):
                return 0

            @staticmethod
            def IsWindowVisible(_hwnd):
                return True

            @staticmethod
            def GetWindowText(hwnd):
                return {7: "MapleStory wiki - 浏览器",
                        9: "冒险岛：怀旧服（新区）攻略"}.get(hwnd, "")

            @staticmethod
            def EnumWindows(callback, extra):
                for hwnd in (7, 9):
                    callback(hwnd, extra)

        sender = WindowKeySender("冒险岛怀旧服", dry_run=True)
        with patch.dict(sys.modules, {"win32gui": FakeWin32Gui}):
            self.assertFalse(sender.game_window_present())

    def test_a_missing_game_window_lists_what_is_on_screen(self) -> None:
        class FakeWin32Gui:
            @staticmethod
            def FindWindow(_class, _title):
                return 0

            @staticmethod
            def IsWindowVisible(_hwnd):
                return True

            @staticmethod
            def GetWindowText(hwnd):
                return {3: "记事本", 4: "浏览器"}.get(hwnd, "")

            @staticmethod
            def EnumWindows(callback, extra):
                for hwnd in (3, 4):
                    callback(hwnd, extra)

        sender = WindowKeySender("冒险岛怀旧服", dry_run=True)
        with patch.dict(sys.modules, {"win32gui": FakeWin32Gui}):
            with self.assertRaises(OSError) as caught:
                sender._find_target_window()
        message = str(caught.exception)
        self.assertIn("found 0", message)
        self.assertIn("记事本", message, "the operator must see what IS on screen")
        self.assertIn("set its window title", message)

    def test_bar_ratios_are_converted_to_values(self) -> None:
        reading = BarStatusDetector().detect(status_image(0.5, 0.2, 0.75))
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)
        self.assertAlmostEqual(reading.exp, 75, delta=3)

    def test_adaptive_full_bar_reference_handles_fixed_pixel_hud(self) -> None:
        # Fixed-pixel HUD: the bars are fixed pixels (164 in the reference
        # capture) while a stale estimate says
        # otherwise.  Once the full bar is observed the reference adapts and
        # ratios are correct - previously every ratio clipped to 1.0 and
        # potions never fired on such machines.
        def frame(hp_px: int, mp_px: int) -> Image.Image:
            image = Image.new("RGB", (CAP_W, CAP_H), "black")
            draw = ImageDraw.Draw(image)
            draw.rectangle((HP_BAR[0], BAND_TOP, HP_BAR[0] + hp_px - 1, BAND_BOTTOM), fill=(220, 20, 20))
            draw.rectangle((MP_BAR[0], BAND_TOP, MP_BAR[0] + mp_px - 1, BAND_BOTTOM), fill=(20, 40, 220))
            return image

        detector = BarStatusDetector()
        first = detector.detect(frame(FULL_W, FULL_W))  # both full: adapts refs
        self.assertAlmostEqual(first.hp, 656, delta=5)
        self.assertAlmostEqual(first.mp, 371, delta=5)
        half = detector.detect(frame(round(FULL_W / 2), FULL_W))   # HP at ~50%
        self.assertAlmostEqual(half.hp, 328, delta=10)
        self.assertAlmostEqual(half.mp, 371, delta=5)

    def test_partial_bar_never_becomes_the_full_reference(self) -> None:
        # A 60px MP fill is only 75% of the conservative 80px reference.  It
        # must remain 75%, not be learned as "full" and reported as 100%.
        image = Image.new("RGB", (CAP_W, CAP_H), "black")
        ImageDraw.Draw(image).rectangle((MP_BAR[0], BAND_TOP, MP_BAR[0] + 59, BAND_BOTTOM), fill=(20, 40, 220))
        detector = BarStatusDetector(replace(
            StatusConfig(status_roi=(0.0, 0.0, 1.0, 1.0)),
            full_bar_width_fractions={
                "hp": 0.224, "mp": 80.0 / CAP_W, "exp": 0.224,
            },
        ))

        reading = detector.detect(image)

        self.assertIsNone(detector._full_run["mp"])
        self.assertAlmostEqual(reading.mp_ratio, 0.75, delta=0.03)

    def test_wide_non_bar_element_does_not_lock_ratio_at_full(self):
        # A wide blue element (HUD frame / bar-track glow) inside the ROI
        # must NOT be measured as the MP bar - it would lock the ratio at
        # 1.0 and MP potions would never fire.  The real fill is used.
        image = Image.new("RGB", (CAP_W, CAP_H), "black")
        draw = ImageDraw.Draw(image)
        # Wide blue artifact, passes the MP mask, sits in its own row band
        # (above the bars) and spans beyond the MP zone.
        draw.rectangle((0, 10, CAP_W - 1, 14), fill=(60, 120, 220))
        # Real MP fill at roughly half length (~82px of the 164px estimate).
        draw.rectangle((MP_BAR[0], BAND_TOP, MP_BAR[0] + 81, BAND_BOTTOM), fill=(20, 40, 220))
        reading = BarStatusDetector().detect(image)
        self.assertIsNotNone(reading.mp_ratio)
        self.assertLess(reading.mp_ratio, 0.7)
        self.assertGreater(reading.mp_ratio, 0.3)

    def test_three_bars_never_mix_each_measured_in_own_zone(self) -> None:
        # The three bars sit side by side in the same vertical band.  The
        # EXP yellow fill must never be measured as HP red, and a saturated
        # yellow-green EXP never as MP blue - each bar is measured only in
        # its own horizontal zone.
        image = Image.new("RGB", (CAP_W, CAP_H), "black")
        draw = ImageDraw.Draw(image)
        # Only EXP is filled (full yellow); HP/MP zones stay empty.
        draw.rectangle((EXP_BAR[0], BAND_TOP, EXP_BAR[1], BAND_BOTTOM),
                       fill=(238, 255, 0))
        reading = BarStatusDetector().detect(image)
        self.assertIsNone(reading.hp_ratio)
        self.assertIsNone(reading.mp_ratio)
        self.assertAlmostEqual(reading.exp_ratio, 1.0, delta=0.05)

        # Only HP is filled (full red); EXP/MP zones stay empty.
        image = Image.new("RGB", (CAP_W, CAP_H), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((HP_BAR[0], BAND_TOP, HP_BAR[1], BAND_BOTTOM),
                       fill=(220, 20, 20))
        reading = BarStatusDetector().detect(image)
        self.assertAlmostEqual(reading.hp_ratio, 1.0, delta=0.05)
        self.assertIsNone(reading.mp_ratio)
        self.assertIsNone(reading.exp_ratio)

        # Only MP is filled (full blue); HP/EXP zones stay empty.
        image = Image.new("RGB", (CAP_W, CAP_H), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((MP_BAR[0], BAND_TOP, MP_BAR[1], BAND_BOTTOM),
                       fill=(20, 40, 220))
        reading = BarStatusDetector().detect(image)
        self.assertIsNone(reading.hp_ratio)
        self.assertAlmostEqual(reading.mp_ratio, 1.0, delta=0.05)
        self.assertIsNone(reading.exp_ratio)

    def test_bar_calibration_survives_left_sixty_percent_capture_crop(self) -> None:
        # The status-only capture (the 538x40 bottom-middle crop) is the image
        # the detector receives; the full bar fractions are fixed-pixel
        # values measured inside that crop.
        full = status_image(0.5, 0.2, 0.75)
        cropped = full.crop((0, 0, CAP_W, CAP_H))
        defaults = StatusConfig()
        config = replace(
            defaults,
            status_roi=(0.0, 0.0, 1.0, 1.0),
        )
        reading = BarStatusDetector(config).detect(cropped)
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)
        self.assertAlmostEqual(reading.exp, 75, delta=3)

    def test_bar_calibration_uses_status_only_capture(self) -> None:
        # The status-only capture (the 538x40 bottom-middle crop) is the image
        # the detector receives; the full bar fractions are fixed-pixel
        # values measured inside that crop.
        full = status_image(0.5, 0.2, 0.75)
        cropped = full.crop((0, 0, CAP_W, CAP_H))
        defaults = StatusConfig()
        config = replace(
            defaults,
            status_roi=(0.0, 0.0, 1.0, 1.0),
        )
        reading = BarStatusDetector(config).detect(cropped)
        self.assertAlmostEqual(reading.hp, 328, delta=5)
        self.assertAlmostEqual(reading.mp, 74, delta=5)
        self.assertAlmostEqual(reading.exp, 75, delta=3)

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

    def test_buff_cast_is_verified_by_the_hud(self) -> None:
        # The sender can only prove the key was emitted; the HUD proves the game
        # consumed it.  A buff that moved HP/MP must be reported as cast.
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event())
        worker._last_hp, worker._last_mp = 552, 349
        worker._arm_buff_verification("buff2", "pagedown")
        with self.assertLogs("status_worker", level="INFO") as captured:
            worker._verify_buff_effects(
                StatusReading(hp=552, mp=330, hp_ratio=1.0, mp_ratio=0.9,
                              confidence=1.0),
                time.monotonic(),
            )
        self.assertIsNone(worker._buff_verification["buff2"])
        self.assertTrue(
            any("cast detected" in line for line in captured.output),
            captured.output,
        )

    def test_buff_that_the_game_ignores_is_reported(self) -> None:
        # The exact field symptom: the log says "executed", the game does
        # nothing.  After the grace window the worker must say so, because the
        # next buff attempt is a whole interval away.
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event())
        worker._last_hp, worker._last_mp = 552, 349
        worker._arm_buff_verification("buff2", "pagedown")
        with self.assertLogs("status_worker", level="WARNING") as captured:
            worker._verify_buff_effects(
                StatusReading(hp=552, mp=349, hp_ratio=1.0, mp_ratio=0.9,
                              confidence=1.0),
                time.monotonic() + 10.0,
            )
        self.assertIsNone(worker._buff_verification["buff2"])
        self.assertTrue(
            any("did not cast anything" in line for line in captured.output),
            captured.output,
        )

    def test_buff_verification_without_hud_values_says_so(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(queue.Queue(), sender, threading.Event())
        worker._arm_buff_verification("buff3", "insert")
        with self.assertLogs("status_worker", level="WARNING") as captured:
            worker._verify_buff_effects(
                StatusReading(hp=None, mp=None, hp_ratio=None, mp_ratio=None,
                              confidence=0.0),
                time.monotonic() + 10.0,
            )
        self.assertTrue(
            any("could not verify" in line for line in captured.output),
            captured.output,
        )

    def test_arbiter_buff_completion_arms_the_verification(self) -> None:
        class QueuedArbiter:
            def __init__(self) -> None:
                self.requests: list[tuple[str, object]] = []

            def request_buff(self, key, on_complete=None):
                self.requests.append((key, on_complete))
                return True

        sender = FakeSender()
        arbiter = QueuedArbiter()
        worker = StatusWorker(queue.Queue(), sender, threading.Event(),
                              motion_arbiter=arbiter)
        worker.detector.config = replace(
            worker.detector.config,
            buff2_key="pagedown", buff2_interval=1.0, buff2_enabled=True,
            buff1_enabled=False, buff3_enabled=False,
        )
        worker._last_buff["buff2"] = time.monotonic() - 10.0
        worker._last_hp, worker._last_mp = 552, 349
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual([key for key, _ in arbiter.requests], ["pagedown"])
        arbiter.requests[0][1](True)
        self.assertIsNotNone(worker._buff_verification["buff2"])

    def test_potion_effect_is_verified_and_retried_once_when_bar_stays_low(self) -> None:
        sender = FakeSender()
        worker = StatusWorker(
            queue.Queue(), sender, threading.Event(),
            potion_cooldown=60, low_frames_required=1,
            potion_verify_seconds=0.25, potion_verify_retries=1,
        )
        low = status_image(0.2, 1.0)
        worker._process_frame(low)
        self.assertEqual(sender.keys, ["delete"])

        # The displayed HP did not rise after the first accepted key press,
        # so the verifier bypasses the normal five-second cooldown once.
        worker._potion_verification["hp"]["deadline"] = time.monotonic() - 0.01
        worker._process_frame(low)
        self.assertEqual(sender.keys, ["delete", "delete"])

        # A real rise confirms the retry and clears the pending verifier.
        worker._process_frame(status_image(0.7, 1.0))
        self.assertIsNone(worker._potion_verification["hp"])

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
        # binding stays.  (``alt`` is a real key but NOT in the whitelist.)
        unchanged = apply_drug_settings(config, {"hp_key": "alt"})
        self.assertEqual(unchanged.hp_key, config.hp_key)

    def test_apply_drug_settings_maps_buff_keys_intervals_and_enabled(self) -> None:
        config = StatusConfig()
        updated = apply_drug_settings(config, {
            "buff1_key": "home", "buff2_key": "space",
            "buff3_key": "pageup",
            "buff1_interval": 10.0, "buff2_interval": 5.5,
            "buff3_interval": 20.0,
            "buff1_enabled": True, "buff2_enabled": True,
            "buff3_enabled": True,
        })
        self.assertEqual(updated.buff1_key, "home")
        self.assertEqual(updated.buff2_key, "space")
        self.assertEqual(updated.buff3_key, "pageup")
        # Minutes in the UI form become seconds in the worker config.
        self.assertAlmostEqual(updated.buff1_interval, 600.0)
        self.assertAlmostEqual(updated.buff2_interval, 330.0)
        self.assertAlmostEqual(updated.buff3_interval, 1200.0)
        self.assertTrue(updated.buff1_enabled)
        self.assertTrue(updated.buff2_enabled)
        self.assertTrue(updated.buff3_enabled)
        # Unbindable key and malformed interval are ignored: defaults stay.
        # (``alt`` is a real key but NOT in the bindable whitelist.)
        ignored = apply_drug_settings(config, {
            "buff1_key": "alt", "buff2_interval": "oops",
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

    def test_queued_buff_restarts_its_timer_only_after_completion(self) -> None:
        sender = FakeSender()
        arbiter = DeferredBuffArbiter()
        worker = StatusWorker(
            queue.Queue(), sender, threading.Event(), motion_arbiter=arbiter,
        )
        worker.detector.config = replace(
            worker.detector.config,
            buff1_enabled=False,
            buff2_key="insert", buff2_interval=60.0, buff2_enabled=True,
        )
        original = time.monotonic() - 61.0
        worker._last_buff["buff2"] = original
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual([key for key, _ in arbiter.requests], ["insert"])
        # A queued request does not reset the deadline or pile up a duplicate.
        self.assertEqual(worker._last_buff["buff2"], original)
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(len(arbiter.requests), 1)
        arbiter.requests[0][1](True)
        self.assertGreater(worker._last_buff["buff2"], original)
        self.assertFalse(worker._buff_pending["buff2"])

    def test_pet_food_is_direct_periodic_drug_not_arbiter_motion(self) -> None:
        sender = FakeSender()
        arbiter = DeferredBuffArbiter()
        worker = StatusWorker(
            queue.Queue(), sender, threading.Event(), motion_arbiter=arbiter,
        )
        worker.detector.config = replace(
            worker.detector.config,
            buff1_key="home", buff1_interval=60.0, buff1_enabled=True,
            buff2_enabled=False, buff3_enabled=False,
        )
        worker._last_buff["buff1"] = time.monotonic() - 61.0
        worker._process_frame(status_image(1.0, 1.0))
        self.assertEqual(sender.keys, ["home"])
        self.assertEqual(arbiter.requests, [])

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

    def test_action_tap_retries_after_a_focus_dip_and_delivers(self) -> None:
        # A tap is the shortest event the bot emits, so a window that steals
        # the foreground for a moment used to swallow it silently while the log
        # still said "executed".
        sender = WindowKeySender("game", dry_run=False)
        focus = [False, True]
        events = []
        sender._foreground_matches = lambda: focus.pop(0) if focus else True
        sender._send_scan_code = lambda code, key_up, extended: events.append(key_up)
        with patch("status_worker.time.sleep") as sleep:
            self.assertTrue(sender.tap("pagedown"))
        self.assertEqual(events, [False, True])          # exactly one tap
        self.assertTrue(sleep.called)                    # the dip was waited out

    def test_action_tap_force_releases_the_key_when_focus_is_stolen_mid_tap(
        self,
    ) -> None:
        # The key-up is global too: if it lands in another window the GAME keeps
        # the key held, and every later tap of that key becomes a no-op.
        sender = WindowKeySender("game", dry_run=False)
        # Focus is fine for the key-down and gone by the end of the hold.
        sender._foreground_matches = lambda: False if sender._used_keys else True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(key_up)
        with patch("status_worker.time.sleep"):
            self.assertFalse(sender.tap("pagedown"))
        # down + up (the lost release) + the forced release, then retries.
        self.assertGreaterEqual(events.count(True), 2)
        self.assertIn("pagedown", sender._used_keys)

    def test_action_tap_never_waits_when_input_is_disarmed(self) -> None:
        sender = WindowKeySender("game", dry_run=False, input_enabled=False)
        sender._send_scan_code = lambda *args, **kwargs: self.fail(
            "no input may be injected while disarmed"
        )
        with patch("status_worker.time.sleep") as sleep:
            self.assertFalse(sender.tap("pagedown"))
        sleep.assert_not_called()

    def test_input_reset_releases_every_key_the_bot_ever_pressed(self) -> None:
        # Movement keys are not the only ones that can stay stuck in the game:
        # a buff key whose key-up was lost makes every later buff a no-op, so a
        # lifecycle scrub must release the whole used-key superset.
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(
            (code, key_up)
        )
        page_down = sender._SCAN["pagedown"][0]
        with patch("status_worker.time.sleep"):
            self.assertTrue(sender.tap("pagedown"))
        events.clear()
        sender.reset_input_session("focus dip")
        self.assertIn((page_down, True), events)
        self.assertEqual(sender._key_owners, {})

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
        # Enabling begins with a deliberate neutral-keyboard scrub.
        events.clear()
        sender._foreground_matches = lambda: True
        self.assertTrue(sender.tap("ctrl"))
        self.assertEqual(events, [False, True])

    def test_explicit_quick_message_works_while_patrol_input_is_disabled(self):
        sender = WindowKeySender("game", dry_run=False, input_enabled=False)
        sender.select_window = lambda: True
        sender.is_game_foreground = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(
            (code, key_up, extended)
        )

        with patch("status_worker.time.sleep"):
            self.assertTrue(sender.send_clipboard_message())

        enter = sender._SCAN["enter"][0]
        ctrl = sender._SCAN["ctrl"][0]
        v_key = sender._SCAN["v"][0]
        self.assertEqual(
            [(code, up) for code, up, _extended in events],
            [
                (enter, False), (enter, True),
                (ctrl, False), (v_key, False), (v_key, True),
                (ctrl, True), (enter, False), (enter, True),
            ],
        )

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

    def test_direction_change_forces_conflicting_key_up_before_new_down(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(
            (code, key_up)
        )

        self.assertTrue(sender.key_down("right"))
        self.assertTrue(sender.key_down("left"))

        right = sender._SCAN["right"][0]
        left = sender._SCAN["left"][0]
        self.assertEqual(events, [
            (right, False), (right, True), (left, False),
        ])
        self.assertFalse(sender.is_key_down("right"))
        self.assertTrue(sender.is_key_down("left"))

    def test_forced_release_emits_key_up_after_ownership_was_forgotten(self) -> None:
        sender = WindowKeySender("game", dry_run=False)
        sender._foreground_matches = lambda: True
        events = []
        sender._send_scan_code = lambda code, key_up, extended: events.append(
            (code, key_up)
        )

        self.assertTrue(sender.key_down("right"))
        sender.release_all_keys(reason="focus dip")
        # The logical table is empty now, but a direction switch must still
        # send a physical right-up in case the game missed the first one.
        self.assertTrue(sender.force_key_up("right", reason="restart scrub"))

        right = sender._SCAN["right"][0]
        self.assertEqual(events.count((right, True)), 2)

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
