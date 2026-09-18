import logging
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import (
    _AnyEvent,
    _capture_focused_game_frame,
    _compact_log_formatter,
    main as assistant_main,
    _start_live_input,
    _stop_live_input,
    parse_args,
    status_capture_pixel_box,
)


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

    def disable_input(self, *, refocus_before_release: bool = False) -> None:
        self.calls.append(("disable", refocus_before_release))


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

    def test_duplicate_launch_explains_that_assistant_is_already_running(self) -> None:
        with patch("assistant._acquire_single_instance_mutex", return_value=None), \
             patch("assistant._show_already_running_notice") as notice:
            self.assertEqual(assistant_main(), 0)
        notice.assert_called_once_with()

    def test_status_capture_is_fast_for_potion_priority(self) -> None:
        with patch("sys.argv", ["assistant.py"]):
            self.assertEqual(parse_args().status_interval, 0.25)

    def test_status_box_is_fixed_pixel_at_or_above_reference_width(self) -> None:
        # 1366px is the HUD reference width; 1920px is above it.  The capture
        # stays 538x40, bottom-anchored, and its centre sits 37px right of the
        # client centre (the new UI's info bar), so 1366x768 and 1920x1080
        # (the same UI preset) return the same box.
        self.assertEqual(
            status_capture_pixel_box((1366, 768)),
            (1366 // 2 - 269 + 37, 768 - 40, 1366 // 2 + 269 + 37, 768),
        )
        self.assertEqual(
            status_capture_pixel_box((1920, 1080)),
            (1920 // 2 - 269 + 37, 1080 - 40, 1920 // 2 + 269 + 37, 1080),
        )

    def test_status_box_is_425x32_on_the_1080x768_preset(self) -> None:
        # The updated UI measures the info bar at 425x32 on the 1080x768
        # preset, bottom-anchored and 29px right of the client centre.
        box = status_capture_pixel_box((1080, 768))
        self.assertEqual(box[2] - box[0], 425)
        self.assertEqual(box[3] - box[1], 32)
        self.assertEqual(box[1], 768 - 32)
        self.assertEqual(box[3], 768)
        self.assertAlmostEqual(
            (box[0] + box[2]) / 2.0, 1080 / 2.0 + 29, delta=1
        )

    def test_status_box_scales_below_reference_width(self) -> None:
        # 1024x768: the whole HUD measures ~0.75x.  The capture scales to
        # round(538*.75)=403 wide and round(40*.75)=30 tall, still
        # bottom-anchored with the proportional centre offset.
        box = status_capture_pixel_box((1024, 768))
        self.assertEqual(box[2] - box[0], 403)
        self.assertEqual(box[3] - box[1], 30)
        self.assertEqual(box[1], 768 - 30)
        self.assertEqual(box[3], 768)
        self.assertAlmostEqual(
            (box[0] + box[2]) / 2.0, 1024 / 2.0 + 28, delta=1
        )

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

    def test_start_reports_success_when_input_is_armed(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        self.assertTrue(_start_live_input(sender, active))
        self.assertEqual(sender.calls, ["select", "verify", "enable"])
        self.assertTrue(active.is_set())

    def test_lie_takeover_veto_never_calibrates_or_arms(self) -> None:
        # 自动过测谎 owns the machine while its sequence runs: a manual Start
        # Patrol must not even calibrate (that draws a layer-band overlay over
        # the lie popup and asks the capture thread for extra frames).
        sender = FakeSender()
        active = threading.Event()
        taken_over = threading.Event()
        taken_over.set()
        prepared = []

        self.assertFalse(_start_live_input(
            sender, active,
            lambda: prepared.append(True),
            veto_event=taken_over,
            veto_reason="自动过测谎 takeover is driving the lie pass",
        ))
        self.assertEqual(sender.calls, ["select", "verify"])
        self.assertEqual(prepared, [])
        self.assertFalse(active.is_set())

    def test_lie_takeover_started_during_calibration_still_arms_nothing(self) -> None:
        # The alarm can arrive while the Start Patrol capture/calibration runs
        # on Tk's thread; arming input afterwards is what let the movement
        # worker walk (and attack) in the middle of a lie pass.
        sender = FakeSender()
        active = threading.Event()
        taken_over = threading.Event()
        preparing = threading.Event()

        def prepare() -> None:
            sender.calls.append("prepare-map")
            taken_over.set()

        self.assertFalse(_start_live_input(
            sender, active, prepare, preparing,
            veto_event=taken_over,
            veto_reason="自动过测谎 takeover is driving the lie pass",
        ))
        self.assertEqual(sender.calls, ["select", "verify", "prepare-map"])
        self.assertFalse(active.is_set())
        self.assertFalse(preparing.is_set())

    def test_stop_disarms_and_requests_game_refocus_for_ui_stop(self) -> None:
        sender = FakeSender()
        active = threading.Event()
        active.set()

        _stop_live_input(sender, active, refocus_before_release=True)

        self.assertFalse(active.is_set())
        self.assertEqual(sender.calls, [("disable", True)])

    def test_recording_capture_focuses_and_settles_before_capture(self) -> None:
        sender = FakeSender()
        calls = []

        with patch("assistant.time.sleep") as sleep:
            frame = _capture_focused_game_frame(
                sender, lambda: calls.append("capture") or "frame"
            )

        self.assertEqual(frame, "frame")
        self.assertEqual(sender.calls, ["select", "verify"])
        sleep.assert_called_once_with(0.08)
        self.assertEqual(calls, ["capture"])


class VersionBannerTests(unittest.TestCase):
    """The log must state which build is running (an old install cost two debugging rounds)."""

    def test_the_startup_banner_names_the_version_and_the_folder(self) -> None:
        source = (Path(__file__).with_name("assistant.py")).read_text(encoding="utf-8")
        self.assertIn("MapleAssistant %s starting from %s", source)
        self.assertIn("from versioning import version_label", source)

    def test_the_version_label_is_read_from_the_version_file(self) -> None:
        from versioning import read_version, version_label

        self.assertEqual(version_label(), f"v{read_version()}")
        self.assertRegex(read_version(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
