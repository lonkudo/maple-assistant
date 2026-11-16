"""Persist only unexpected Python tracebacks for post-crash diagnosis.

This is intentionally separate from the normal asynchronous log streams.  A
report is written only when an uncaught exception reaches a process, worker,
or Tk boundary, then the file is flushed once so it remains available after
the dashboard has disappeared.  No timer or normal gameplay event writes to
this file.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from runtime_paths import application_root


_WRITE_LOCK = threading.Lock()


def crash_report_path() -> Path:
    """Return the crash report that survives normal log cleanup on launch."""

    return application_root() / "work" / "crash_report.log"


def record_exception(
    context: str,
    exc_type: Type[BaseException] | None,
    exc_value: BaseException | None,
    exc_traceback: TracebackType | None,
) -> None:
    """Append and synchronously persist one traceback without using logging."""

    if exc_type is None or issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        return
    try:
        report = crash_report_path()
        report.parent.mkdir(parents=True, exist_ok=True)
        heading = (
            "\n"
            + "=" * 76
            + f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} | {context}"
            + f" | thread={threading.current_thread().name}\n"
        )
        body = "".join(traceback.format_exception(
            exc_type, exc_value, exc_traceback,
        ))
        payload = (heading + body).encode("utf-8", errors="backslashreplace")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        with _WRITE_LOCK:
            descriptor = os.open(str(report), flags, 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        break
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        # The original crash must keep its normal Python handling even if the
        # report folder itself is unavailable or the volume is failing.
        pass


def record_current_exception(context: str) -> None:
    """Persist the exception currently being handled by an ``except`` block."""

    exc_type, exc_value, exc_traceback = sys.exc_info()
    record_exception(context, exc_type, exc_value, exc_traceback)


# faulthandler needs the stream to outlive the call, so the handle is kept here
# for the whole process.  A local would be collected and the dump would go to a
# closed file - or nowhere - exactly when it is needed.
_FATAL_STREAM = None


def install_fatal_handler(path: Path | None = None) -> bool:
    """Dump a traceback on a native fault, not only on a Python exception.

    ``sys.excepthook`` / ``threading.excepthook`` cover exceptions.  They cannot
    see an access violation inside a compiled extension, a Tcl abort, or a fatal
    interpreter error - the cases where the window disappears and NOTHING is
    written ("the instance started after the update flashes closed").  Windows
    reports such a death to WER, not to the install folder, so without this the
    only record is the absence of the next line in ``assistant.log``.

    The dump is appended to ``work/crash_report.log`` beside the exception
    reports (same file, same "read it after the fact" role).  Returns whether
    the handler was installed; a failure is never fatal.
    """

    global _FATAL_STREAM
    if _FATAL_STREAM is not None:
        return True
    try:
        import faulthandler

        report = Path(path) if path is not None else crash_report_path()
        report.parent.mkdir(parents=True, exist_ok=True)
        # Line buffered and append-only: a crash must never erase the report of
        # the previous one, and the last line has to reach the disk as it is
        # written instead of sitting in a buffer the dead process cannot flush.
        stream = report.open("a", encoding="utf-8", errors="backslashreplace",
                             buffering=1)
        stream.write(
            "\n" + "=" * 76
            + f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} | fatal-signal handler armed"
            + f" | pid={os.getpid()}\n"
        )
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
        _FATAL_STREAM = stream
        return True
    except Exception:
        return False


__all__ = [
    "crash_report_path", "install_fatal_handler", "record_current_exception",
    "record_exception",
]
