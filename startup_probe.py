"""Hidden-launch entry point that records startup failures for the user.

``启动助手.bat`` intentionally uses ``pythonw.exe`` so no command window
flashes.  The trade-off is that an import/configuration exception would be
invisible.  This wrapper writes the traceback beside the launcher, then starts
the normal assistant unchanged.

A missing third-party dependency is repaired instead of only being reported:
the probe runs the package installer once, then starts the assistant again in
a fresh process.  A frozen EXE cannot do this (its Python runtime is embedded),
which is why ``build_protected_release.ps1`` bundles and verifies every
requirement at build time.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import traceback


LOG_PATH = Path(__file__).with_name("assistant-launch-error.log")
STATUS_PATH = Path(__file__).with_name("assistant-launch-status.log")
ERROR_LOG_PATH = Path(__file__).with_name("error.log")
INSTALLER_NAME = "安装.bat"
REPAIR_FLAG = "MAPLE_ASSISTANT_DEPENDENCY_REPAIR"
REPAIR_TIMEOUT_SECONDS = 1800


def _write_error(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    # Keep the legacy launch log, but write every fatal traceback to the
    # simple error.log requested for normal troubleshooting.
    for path in (LOG_PATH, ERROR_LOG_PATH):
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


def _write_status(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with STATUS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _notify(message: str) -> None:
    """Show a message box; pythonw has no console to print to."""

    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MapleAssistant", 0x10)
    except Exception:
        pass


def _missing_dependency(exc: BaseException) -> str:
    """Return the missing third-party module name, or an empty string."""

    if not isinstance(exc, ModuleNotFoundError):
        return ""
    name = str(getattr(exc, "name", "") or "").split(".")[0]
    if not name:
        return ""
    # A missing *local* module is a damaged package, not an installable
    # requirement, so the installer cannot repair it.
    if (Path(__file__).parent / f"{name}.py").exists():
        return ""
    if (Path(__file__).parent / name / "__init__.py").exists():
        return ""
    return name


def _repair_dependencies(missing: str) -> bool:
    """Run the package installer once so the missing module can be installed."""

    installer = Path(__file__).with_name(INSTALLER_NAME)
    if not installer.exists():
        _write_error(
            f"Missing dependency '{missing}' and no installer ({INSTALLER_NAME}) "
            "is present beside the launcher. Reinstall the package."
        )
        return False
    _write_error(
        f"Missing dependency '{missing}'. Running {INSTALLER_NAME} automatically "
        "to install the requirements."
    )
    _write_status(f"Automatic dependency repair started for '{missing}'.")
    try:
        process = subprocess.Popen(
            ["cmd", "/c", str(installer)],
            cwd=str(installer.parent),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        code = process.wait(timeout=REPAIR_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        _write_error(f"Automatic dependency repair could not run: {exc!r}")
        return False
    if code != 0:
        _write_error(f"Automatic dependency repair failed with exit code {code}.")
        return False
    _write_status("Automatic dependency repair finished; starting the assistant again.")
    return True


def _restart_probe() -> int:
    """Start this probe again in a fresh process that can import the new modules."""

    environment = dict(os.environ)
    environment[REPAIR_FLAG] = "1"
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(Path(__file__).parent),
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _write_error(f"Assistant could not be restarted after repair: {exc!r}")
        return 1
    return 0


def _run_assistant() -> int:
    _write_status("Python startup probe reached.")
    from assistant import main as run_assistant

    result = run_assistant()
    code = int(result or 0)
    if code:
        _write_error(f"Assistant exited during startup with code {code}.")
    else:
        _write_status("Assistant exited normally.")
    return code


def main() -> int:
    try:
        return _run_assistant()
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:
        _write_error("Assistant failed during startup:\n" + traceback.format_exc())
        missing = _missing_dependency(exc)
        if not missing:
            # Unchanged behaviour for every other failure: the traceback is in
            # assistant-launch-error.log, and no dialog blocks an unattended
            # start.
            return 1
        if os.environ.get(REPAIR_FLAG) == "1":
            _notify(
                f"缺少依赖 {missing}，自动安装后仍未成功。\n"
                f"Missing dependency '{missing}' is still unavailable after the "
                "automatic install. Please send assistant-launch-error.log to the developer."
            )
            return 1
        if not _repair_dependencies(missing):
            _notify(
                f"缺少依赖 {missing}，自动安装没有完成。\n"
                f"Double-click 安装.bat, then start the assistant again.\n"
                f"Missing dependency: {missing}"
            )
            return 1
        return _restart_probe()


if __name__ == "__main__":
    raise SystemExit(main())
