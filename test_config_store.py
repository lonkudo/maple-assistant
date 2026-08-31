import json
from pathlib import Path
import tempfile
import unittest

from config_store import (
    ConfigSectionFile,
    ConfigStore,
    DEFAULT_SYSTEM_CONFIG,
)


class ConfigStoreTests(unittest.TestCase):
    def _store(self, root: Path) -> ConfigStore:
        return ConfigStore(
            root / "user_config.json",
            system_path=root / "system_config.json",
            legacy_unified_path=root / "config.json",
        )

    def test_migrates_legacy_files_into_user_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "drug_settings.json").write_text(
                json.dumps({"hp_key": "delete", "hp_threshold": 61}),
                encoding="utf-8",
            )
            (root / "fixed_attack_settings.json").write_text(
                json.dumps({"interval_seconds": .9}), encoding="utf-8"
            )
            store = self._store(root)
            document = json.loads(
                (root / "user_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(document["drug"]["hp_threshold"], 61)
            self.assertEqual(document["fixed_attack"]["interval_seconds"], .9)
            self.assertIn("recording", document)
            self.assertNotIn("rope_calibration", document)
            self.assertEqual(
                store.read_section("rope_calibration")[
                    "stair_jump_stall_frames"
                ],
                10,
            )

    def test_unified_config_migrates_only_user_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({
                "fixed_attack": {"interval_seconds": 1.7},
                "rope_calibration": {"stair_jump_stall_frames": 6},
            }), encoding="utf-8")
            (root / "system_config.json").write_text(json.dumps({
                "rope_calibration": {"stair_jump_stall_frames": 10},
            }), encoding="utf-8")

            store = self._store(root)
            user_document = json.loads(
                (root / "user_config.json").read_text(encoding="utf-8")
            )

            self.assertEqual(
                store.read_section("fixed_attack")["interval_seconds"], 1.7
            )
            self.assertNotIn("rope_calibration", user_document)
            self.assertEqual(
                store.read_section("rope_calibration")[
                    "stair_jump_stall_frames"
                ],
                10,
            )

    def test_section_write_preserves_every_other_user_section(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            original_recording = store.read_section("recording")
            section = ConfigSectionFile(store, "drug")
            section.write_text(json.dumps({"hp_key": "home"}))
            self.assertEqual(store.read_section("drug"), {"hp_key": "home"})
            self.assertEqual(store.read_section("recording"), original_recording)

    def test_existing_user_config_wins_over_stale_unified_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "user_config.json").write_text(
                json.dumps({"fixed_attack": {"interval_seconds": 1.7}}),
                encoding="utf-8",
            )
            (root / "config.json").write_text(
                json.dumps({"fixed_attack": {"interval_seconds": 9.9}}),
                encoding="utf-8",
            )
            store = self._store(root)
            self.assertEqual(
                store.read_section("fixed_attack")["interval_seconds"], 1.7
            )

    def test_system_section_is_read_only_at_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "system_config.json").write_text(json.dumps({
                "rope_calibration": {"stair_jump_stall_frames": 10},
            }), encoding="utf-8")
            store = self._store(root)

            with self.assertRaises(PermissionError):
                store.write_section(
                    "rope_calibration", {"stair_jump_stall_frames": 3}
                )

    def test_recording_can_replace_user_minimap_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            original_recording = store.read_section("recording")

            calibration = {"schema": 1, "window_box": [0, 0, .1, .2]}
            store.write_section("minimap_calibration", calibration)

            self.assertEqual(
                store.read_section("minimap_calibration"), calibration
            )
            self.assertEqual(
                store.read_section("recording"), original_recording
            )

    def test_tracked_system_file_matches_code_fallback(self):
        tracked = json.loads(
            Path(__file__).with_name("system_config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(tracked, DEFAULT_SYSTEM_CONFIG)


if __name__ == "__main__":
    unittest.main()
