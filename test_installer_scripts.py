import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent


class InstallerScriptTests(unittest.TestCase):
    def test_batch_requests_uac_before_installation(self):
        text = (ROOT / "安装.bat").read_text(encoding="utf-8-sig")
        lowered = text.lower()
        self.assertIn("-verb runas", lowered)
        self.assertIn("--elevated", lowered)
        self.assertLess(lowered.index("-verb runas"), lowered.index("install.ps1"))

    def test_python_installer_is_silent_and_hidden(self):
        text = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
        lowered = text.lower()
        self.assertIn("/quiet", lowered)
        self.assertIn("-windowstyle hidden", lowered)
        self.assertIn("--disable-interactivity", lowered)
        self.assertLess(lowered.index("python.org/ftp"), lowered.index("winget install"))

    def test_assistant_launcher_cannot_relaunch_batch_recursively(self):
        for name in ("start_assistant.bat", "启动助手.bat"):
            text = (ROOT / name).read_text(encoding="utf-8-sig").lower()
            self.assertIn("wscript.exe", text)
            self.assertNotIn("net session", text)
            self.assertNotIn("start-process", text)
        vbs = (ROOT / "launch_assistant.vbs").read_text(
            encoding="utf-8-sig"
        ).lower()
        self.assertIn("shellapp.shellexecute", vbs)
        self.assertIn('"runas", 0', vbs)
        self.assertIn("pythonw.exe", vbs)
        self.assertIn("startup_probe.py", vbs)
        self.assertIn("assistant-launch-status.log", vbs)


if __name__ == "__main__":
    unittest.main()
