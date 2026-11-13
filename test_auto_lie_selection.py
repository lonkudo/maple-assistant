"""自动测谎 selection: the radio group and the armed flag are one selection.

Field report: the panel **looked** like 云端测谎 was selected while the feature was
actually off, so every real lie window was ignored.  Two paths produced that:

* the settings-restore path forced the radio group to ``cloud`` whenever the saved
  backend was absent/unknown, even with ``auto_lie_api_enabled = false``;
* switching 测谎 off disarmed the boolean but left the group on its backend.

``auto_lie_radio_value()`` is now the single source of truth for the pair, so a
disarmed selection can only show 关闭.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ui_worker import (
    AUTOLIE_BACKEND_CLOUD,
    AUTOLIE_BACKEND_LOCAL,
    AUTOLIE_BACKEND_OFF,
    auto_lie_radio_value,
)


class AutoLieRadioValueTests(unittest.TestCase):
    def test_a_disarmed_selection_can_only_show_off(self) -> None:
        for saved in ("cloud", "local", "", None, "CLOUD", "junk", "cloudy"):
            with self.subTest(saved=saved):
                self.assertEqual(
                    auto_lie_radio_value(False, saved), AUTOLIE_BACKEND_OFF
                )

    def test_an_armed_selection_keeps_its_backend(self) -> None:
        self.assertEqual(
            auto_lie_radio_value(True, "cloud"), AUTOLIE_BACKEND_CLOUD
        )
        self.assertEqual(
            auto_lie_radio_value(True, "local"), AUTOLIE_BACKEND_LOCAL
        )
        # case and stray whitespace from a hand-edited config
        self.assertEqual(
            auto_lie_radio_value(True, " LOCAL "), AUTOLIE_BACKEND_LOCAL
        )

    def test_an_armed_selection_without_a_backend_key_defaults_to_cloud(self) -> None:
        # Compatibility rule: every version before the backend split ran the cloud
        # pass, and such a config has no ``auto_lie_backend`` key at all.  It must
        # keep running the cloud pass - not silently fall to 关闭.
        for saved in ("", None, "junk", "cloudy", "off"):
            with self.subTest(saved=saved):
                self.assertEqual(
                    auto_lie_radio_value(True, saved), AUTOLIE_BACKEND_CLOUD
                )

    def test_the_loader_and_the_radio_use_the_same_rule(self) -> None:
        # _load_api_auto_lie_backend_setting() documents the same mapping; both
        # reading the same file must not disagree about an unknown value.
        from ui_worker import UiWorker

        for raw in ("", None, "local", "LOCAL", "cloud", "junk"):
            data = {"auto_lie_backend": raw} if raw is not None else {}
            with self.subTest(raw=raw), \
                    mock.patch("pathlib.Path.read_text",
                               return_value=json.dumps(data)):
                loader = UiWorker._load_api_auto_lie_backend_setting(
                    type("S", (), {"_shutdown_settings_path": lambda _self: Path("x")})()
                )
            self.assertEqual(loader, auto_lie_radio_value(True, raw))

    def test_the_values_are_the_ones_the_radios_use(self) -> None:
        self.assertEqual(AUTOLIE_BACKEND_OFF, "off")
        self.assertEqual(AUTOLIE_BACKEND_CLOUD, "cloud")
        self.assertEqual(AUTOLIE_BACKEND_LOCAL, "local")


if __name__ == "__main__":
    unittest.main()
