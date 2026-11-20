"""Persist only unexpected Python tracebacks for post-crash diagnosis.

This is intentionally separate from the normal asynchronous log streams.  A
report is written only when an uncaught exception reaches a process, worker,
or Tk boundary, then the file is flushed once so it remains available after
the dashboard has disappeared.  No timer or normal gameplay event writes to
this file.
"""

from __future__ import annotations

import json
import logging
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


def install_run_marker(root: Path | None = None, *, version: str = "") -> Optional[dict]:
    """Record this run and report the previous one if it died without a clean exit.

    An intermittent "it closed by itself" has no trace today: a native fault
    writes a faulthandler dump ONLY when there is one (an external termination,
    a TerminateProcess, or a death so early that the dump cannot be written leaves
    nothing), the Python log ends mid-sentence, and the next launch clears the
    logs.  This marker makes each such event countable and datable:

    * ``work/run-marker.json`` is written at startup and stamped ``clean`` by
      ``mark_clean_shutdown``;
    * a start that finds a marker from ANOTHER pid without that stamp appends one
      line to ``work/unclean-exits.log`` - time, version, the old pid/start and
      the last line of ``work/assistant.log`` (usually the exact place where it
      died).

    Both files are in the log-cleanup keep list, so the evidence survives.
    """

    work = (Path(root) if root is not None else application_root()) / "work"
    marker_path = work / "run-marker.json"
    previous: Optional[dict] = None
    try:
        work.mkdir(parents=True, exist_ok=True)
        if marker_path.is_file():
            try:
                previous = json.loads(marker_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a broken marker is not fatal
                previous = None
        if (
            isinstance(previous, dict)
            and not previous.get("clean")
            and int(previous.get("pid") or 0) != os.getpid()
        ):
            _append_unclean_exit(work, previous)
        marker_path.write_text(
            json.dumps({
                "pid": os.getpid(),
                "started": time.time(),
                "version": version,
                "exe": sys.executable,
                "clean": False,
            }, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - diagnostics only
        pass
    return previous


def _append_unclean_exit(work: Path, previous: dict) -> None:
    """One line per run that ended without calling ``mark_clean_shutdown``."""

    try:
        started = float(previous.get("started") or 0.0)
        started_text = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
            if started else "?"
        )
        last_line = ""
        log_path = work / "assistant.log"
        if log_path.is_file():
            try:
                with log_path.open("r", encoding="utf-8", errors="backslashreplace") as handle:
                    for line in handle:
                        if line.strip():
                            last_line = line.strip()
            except Exception:  # noqa: BLE001
                last_line = ""
        payload = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] previous run ended without a "
            f"clean shutdown: pid={previous.get('pid')} version="
            f"{previous.get('version') or '?'} started={started_text} "
            f"exe={previous.get('exe') or '?'}\n"
            f"    last log line: {last_line[:300] or '(none)'}\n"
        )
        with (work / "unclean-exits.log").open("a", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:  # noqa: BLE001
        pass


def mark_clean_shutdown(root: Path | None = None) -> None:
    """Stamp the run marker so the next start does not call this run a crash."""

    work = (Path(root) if root is not None else application_root()) / "work"
    marker_path = work / "run-marker.json"
    try:
        data = {}
        if marker_path.is_file():
            try:
                data = json.loads(marker_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        if int(data.get("pid") or 0) not in (0, os.getpid()):
            return
        data.update({"clean": True, "ended": time.time()})
        marker_path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    except Exception:  # noqa: BLE001 - a shutdown must never fail on this
        pass


def enable_local_dumps(root: Path | None = None) -> bool:
    """Ask Windows Error Reporting to keep a minidump for this image.

    A native fault in a compiled extension leaves "the window disappeared" and,
    at best, a faulthandler dump that names no module.  ``LocalDumps`` is the only
    way to get the faulting module and offset, and it must live under HKLM, which
    this app can write because it already runs elevated.  Best effort: a missing
    elevation or a locked-down policy only logs.
    """

    try:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            return False
    except Exception:  # noqa: BLE001 - not Windows, or no shell32
        return False
    try:
        import winreg

        work = (Path(root) if root is not None else application_root()) / "work"
        dump_dir = work / "dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)
        images = {Path(sys.executable).name or ""}
        images.discard("")
        images.update({"MoonEclipse.exe", "TodoHelper.exe"})
        base = (
            r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\\"
        )
        written = []
        for image in sorted(images):
            try:
                with winreg.CreateKeyEx(
                    winreg.HKEY_LOCAL_MACHINE, base + image, 0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.SetValueEx(
                        key, "DumpFolder", 0, winreg.REG_EXPAND_SZ, str(dump_dir),
                    )
                    winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 3)
                written.append(image)
            except OSError:
                continue
        if written:
            try:
                logging.getLogger(__name__).info(
                    "crash dumps enabled for %s -> %s",
                    ", ".join(written), dump_dir,
                )
            except Exception:  # noqa: BLE001
                pass
            return True
    except Exception:  # noqa: BLE001 - diagnostics only
        return False
    return False


__all__ = [
    "crash_report_path", "enable_local_dumps", "install_fatal_handler",
    "install_run_marker", "mark_clean_shutdown", "record_current_exception",
    "record_exception",
]
