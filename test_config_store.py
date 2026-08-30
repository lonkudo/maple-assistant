import json
from pathlib import Path
import tempfile
import unittest

from config_store import ConfigSectionFile, ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def test_migrates_legacy_files_into_one_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "drug_settings.json").write_text(
                json.dumps({"hp_key": "delete", "hp_threshold": 61}),
                encoding="utf-8",
            )
            (root / "fixed_attack_settings.json").write_text(
                json.dumps({"interval_seconds": .9}), encoding="utf-8"
            )
            store = ConfigStore(root / "config.json")
            document = json.loads(
                (root / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(document["drug"]["hp_threshold"], 61)
            self.assertEqual(document["fixed_attack"]["interval_seconds"], .9)
            self.assertIn("recording", document)

    def test_section_write_preserves_every_other_section(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            original_recording = store.read_section("recording")
            section = ConfigSectionFile(store, "drug")
            section.write_text(json.dumps({"hp_key": "home"}))
            self.assertEqual(store.read_section("drug"), {"hp_key": "home"})
            self.assertEqual(store.read_section("recording"), original_recording)

    def test_existing_config_wins_over_stale_legacy_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps({"fixed_attack": {"interval_seconds": 1.7}}),
                encoding="utf-8",
            )
            (root / "fixed_attack_settings.json").write_text(
                json.dumps({"interval_seconds": 9.9}), encoding="utf-8"
            )
            store = ConfigStore(root / "config.json")
            self.assertEqual(
                store.read_section("fixed_attack")["interval_seconds"], 1.7
            )

    def test_old_stair_frame_default_is_migrated_to_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "rope_calibration": {"stair_jump_stall_frames": 6},
            }), encoding="utf-8")

            store = ConfigStore(path)

            self.assertEqual(
                store.read_section("rope_calibration")[
                    "stair_jump_stall_frames"
                ],
                10,
            )

    def test_custom_stair_frame_setting_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "rope_calibration": {"stair_jump_stall_frames": 14},
            }), encoding="utf-8")

            store = ConfigStore(path)

            self.assertEqual(
                store.read_section("rope_calibration")[
                    "stair_jump_stall_frames"
                ],
                14,
            )


if __name__ == "__main__":
    unittest.main()
