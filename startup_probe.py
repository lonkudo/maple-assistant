"""Hidden-launch entry point that records startup failures for the user.

``启动助手.bat`` intentionally uses ``pythonw.exe`` so no command window
flashes.  The trade-off is that an import/configuration exception would be
invisible.  This tiny wrapper writes the traceback beside the launcher, then
starts the normal assistant unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import traceback


LOG_PATH = Path(__file__).with_name("assistant-launch-error.log")


def _write_error(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def main() -> int:
    try:
        from assistant import main as run_assistant

        result = run_assistant()
        code = int(result or 0)
        if code:
            _write_error(f"Assistant exited during startup with code {code}.")
        return code
    except KeyboardInterrupt:
        return 0
    except BaseException:
        _write_error("Assistant failed during startup:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
