import tempfile
from pathlib import Path
import unittest
from unittest import mock
import zipfile

from update_manager import (
    UpdateError, apply_desktop_update, export_user_config, find_newer_desktop_update,
    remove_consumed_update_package, schedule_hidden_restart,
)


class DesktopUpdateTests(unittest.TestCase):
    def _zip_release(self, desktop: Path, version: str, *, config: bool = False) -> Path:
        package = desktop / f"MapleAssistant-{version}.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("MapleAssistant/assistant.py", f"VERSION = '{version}'\n")
            archive.writestr("MapleAssistant/VERSION", f"{version}\n")
            archive.writestr("MapleAssistant/new_file.txt", "updated")
            if config:
                archive.writestr("MapleAssistant/user_config.json", '{"from": "update"}')
        return package

    def test_finds_highest_valid_newer_desktop_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp)
            self._zip_release(desktop, "1.0.1")
            newest = self._zip_release(desktop, "1.0.3")
            self.assertEqual(
                find_newer_desktop_update("1.0.0", [desktop]).path, newest
            )

    def test_finds_newer_zip_in_install_parent_without_recursive_drive_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            desktop = root / "Desktop"
            desktop.mkdir()
            install = root / "running"
            install.mkdir()
            package = self._zip_release(root, "1.0.3")
            self.assertEqual(
                find_newer_desktop_update(
                    "1.0.0", [desktop], local_roots=[install, root]
                ).path,
                package,
            )

    def test_update_copies_package_and_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            desktop = root / "Desktop"
            desktop.mkdir()
            package_path = self._zip_release(desktop, "1.0.2", config=True)
            install = root / "running"
            install.mkdir()
            (install / "assistant.py").write_text("old", encoding="utf-8")
            (install / "VERSION").write_text("1.0.0\n", encoding="ascii")
            package = find_newer_desktop_update("1.0.0", [desktop])
            self.assertEqual(package.path, package_path)
            result = apply_desktop_update(package, install)
            self.assertEqual(result.package.version, "1.0.2")
            self.assertTrue(result.config_copied)
            self.assertEqual((install / "VERSION").read_text(encoding="ascii").strip(), "1.0.2")
            self.assertEqual((install / "user_config.json").read_text(encoding="utf-8"), '{"from": "update"}')

    def test_successful_update_zip_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp)
            package_path = self._zip_release(desktop, "1.0.2")
            package = find_newer_desktop_update("1.0.0", [desktop])
            self.assertTrue(remove_consumed_update_package(package))
            self.assertFalse(package_path.exists())

    def test_no_newer_package_reports_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp)
            self._zip_release(desktop, "1.0.0")
            with self.assertRaises(UpdateError):
                find_newer_desktop_update("1.0.0", [desktop])

    def test_matching_config_version_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            desktop = root / "Desktop"
            desktop.mkdir()
            package = desktop / "MapleAssistant-1.0.2.zip"
            config = '{"user_config_updated_at":"2026-09-05T08:00:00Z","from":"desktop"}'
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("MapleAssistant/assistant.py", "new")
                archive.writestr("MapleAssistant/VERSION", "1.0.2\n")
                archive.writestr("MapleAssistant/user_config.json", config)
            install = root / "running"
            install.mkdir()
            (install / "assistant.py").write_text("old")
            (install / "VERSION").write_text("1.0.0\n")
            installed = '{"user_config_updated_at":"2026-09-05T08:00:00Z","from":"running"}'
            (install / "user_config.json").write_text(installed)
            result = apply_desktop_update(
                find_newer_desktop_update("1.0.0", [desktop]), install
            )
            self.assertFalse(result.config_copied)
            self.assertTrue(result.config_unchanged)
            self.assertEqual((install / "user_config.json").read_text(), installed)

    def test_export_overwrites_desktop_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            desktop = root / "Desktop"
            desktop.mkdir()
            source = root / "user_config.json"
            source.write_text('{"user_config_updated_at":"tag","x":1}')
            target = export_user_config(source, [desktop])
            self.assertEqual(target, desktop / "user_config.json")
            self.assertEqual(target.read_text(), source.read_text())

    def test_restart_helper_waits_for_current_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "launch_assistant.vbs").write_text("' launcher", encoding="utf-8")
            with mock.patch("update_manager.subprocess.Popen") as popen:
                helper = schedule_hidden_restart(root, delay_ms=300)
            self.assertTrue(helper.is_file())
            content = helper.read_text(encoding="utf-16")
            self.assertIn("Win32_Process", content)
            self.assertIn("launch_assistant.vbs", content)
            popen.assert_called_once()
            helper.unlink()


if __name__ == "__main__":
    unittest.main()
