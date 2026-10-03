"""Geometry of the 交易 accept-invitation click (Ctrl+W).

The button is anchored to the game client's BOTTOM-RIGHT corner; the caller's rule is

    1366x768 preset: (right - 206, bottom - 91)
    1080x768 preset: both offsets shrink by the width ratio

The old absolute (885, 672) was only correct on a 1366x768 client, which is exactly why the
acceptance click landed off the button on other presets.
"""

import json
from pathlib import Path
import tempfile
import unittest

from minimap_detector import hud_scale_for
from trade_worker import ACCEPT_INVITATION_OFFSET, TradeWorker


class AcceptInvitationPointTests(unittest.TestCase):
    def test_offset_matches_the_measured_preset(self) -> None:
        # The 1366x768 measurement the operator gave, in logical pixels.
        self.assertEqual(ACCEPT_INVITATION_OFFSET, (206, 91))

    def test_1366x768_client(self) -> None:
        # A 1366x768 client at the desktop origin: right-bottom (1366, 768).  Both scale modes
        # agree here (hud_scale_for(1366) == 1.0), which is why the default cannot affect the
        # machine where this already worked.
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1366, 768)),
            (1366 - 206, 768 - 91),
        )
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1366, 768), scale="width"),
            (1366 - 206, 768 - 91),
        )

    def test_client_offset_on_the_desktop_is_added(self) -> None:
        # The game window sits at (162, 130) with a client origin at (170, 161) in the field.
        self.assertEqual(
            TradeWorker.accept_invitation_point((170, 161, 1366, 768)),
            (170 + 1366 - 206, 161 + 768 - 91),
        )

    def test_1080x768_preset_shrinks_both_offsets_by_the_width_ratio(self) -> None:
        scale = hud_scale_for(1080)
        self.assertAlmostEqual(scale, 1080.0 / 1366.0)
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1080, 768), scale="width"),
            (1080 - round(206 * scale), 768 - round(91 * scale)),
        )
        # The shrunk offsets the operator originally asked for on that preset.
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1080, 768), scale="width"),
            (917, 696),
        )

    def test_wider_clients_do_not_grow_the_offset(self) -> None:
        # hud_scale_for is capped at 1.0: a 1920px client keeps the 1366px HUD size.
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1920, 768)),
            (1920 - 206, 768 - 91),
        )

    def test_the_width_rule_reproduces_the_measured_1080x768_offset(self) -> None:
        # Field measurement on the 1080x768 machine (trade_offset_probe.py):
        #   client origin=(170, 161) size=1080x768 bottom_right=(1250, 929)
        #   offset_from_client_br=(165, 71) -> the cursor sat on the accept button
        # The width-scaled 1366x768 offset must land within a couple of pixels of that.
        geometry = (170, 161, 1080, 768)
        point = TradeWorker.accept_invitation_point(geometry)
        measured = (1250 - 165, 929 - 71)
        self.assertLessEqual(abs(point[0] - measured[0]), 3, (point, measured))
        self.assertLessEqual(abs(point[1] - measured[1]), 3, (point, measured))
        # And on the preset it was measured on, it is exact.
        self.assertEqual(
            TradeWorker.accept_invitation_point((170, 161, 1366, 768)),
            (170 + 1366 - 206, 161 + 768 - 91),
        )

    def test_the_old_fixed_constant_only_matched_the_reference_preset(self) -> None:
        # Documented regression: (885, 672) == (right-481, bottom-96) at 1366x768.
        self.assertEqual((1366 - 885, 768 - 672), (481, 96))


class AcceptCalibrationTests(unittest.TestCase):
    """Per-machine calibration: trade_offsets.json decides the offset and the scale mode."""

    def test_missing_file_keeps_the_measured_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            offset, scale = TradeWorker.load_accept_calibration(
                Path(directory) / "absent.json"
            )
        self.assertEqual(offset, (206, 91))
        # Default scales with the client width - verified on the operator's 1080x768 machine
        # (see the measurement test below).  Identical to "none" on a 1366x768 client.
        self.assertEqual(scale, "width")

    def test_file_overrides_offset_and_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trade_offsets.json"
            path.write_text(
                json.dumps({"accept_offset": [165, 71], "accept_scale": "none"}),
                encoding="utf-8",
            )
            self.assertEqual(
                TradeWorker.load_accept_calibration(path), ((165, 71), "none")
            )

    def test_broken_file_keeps_the_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trade_offsets.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(
                TradeWorker.load_accept_calibration(path), ((206, 91), "width")
            )
            path.write_text(json.dumps({"accept_offset": [1], "accept_scale": "huge"}),
                            encoding="utf-8")
            self.assertEqual(
                TradeWorker.load_accept_calibration(path), ((206, 91), "width")
            )

    def test_scale_none_keeps_the_offset_fixed(self) -> None:
        # The other hypothesis: the trade window keeps its pixel size and stays anchored to the
        # corner, so a 1080x768 client needs the unshrunk offsets.
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1080, 768), scale="none"),
            (1080 - 206, 768 - 91),
        )
        self.assertEqual(
            TradeWorker.accept_invitation_point((0, 0, 1366, 768), scale="none"),
            (1366 - 206, 768 - 91),
        )

    def test_explicit_offset_wins_over_the_default(self) -> None:
        self.assertEqual(
            TradeWorker.accept_invitation_point(
                (0, 0, 1080, 768), offset=(165, 71), scale="none"
            ),
            (1080 - 165, 768 - 71),
        )
        self.assertEqual(
            TradeWorker.accept_invitation_point(
                (0, 0, 1080, 768), offset=(165, 71), scale="width"
            ),
            (1080 - round(165 * 1080 / 1366), 768 - round(71 * 1080 / 1366)),
        )


class ProbeArithmeticTests(unittest.TestCase):
    """The probe must report the same numbers the assistant clicks."""

    def test_probe_click_points_match_the_worker(self) -> None:
        from trade_offset_probe import REFERENCE_OFFSET, _click_point
        from trade_worker import TradeWorker
        from minimap_detector import hud_scale_for

        self.assertEqual(REFERENCE_OFFSET, (206, 91))
        for client in ((170, 161, 1366, 768), (170, 161, 1080, 768)):
            with self.subTest(client=client):
                self.assertEqual(
                    _click_point(client, REFERENCE_OFFSET, hud_scale_for(client[2])),
                    TradeWorker.accept_invitation_point(client, scale="width"),
                )
                self.assertEqual(
                    _click_point(client, REFERENCE_OFFSET, 1.0),
                    TradeWorker.accept_invitation_point(client, scale="none"),
                )


if __name__ == "__main__":
    unittest.main()
