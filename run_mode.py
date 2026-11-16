"""Debug / produce switch for the temporary test facilities.

``mode.txt`` next to this module holds exactly one digit:

    0 = debug    developer testing: shortened minimums used while testing a
                 feature on screen (for 重置花 the minimum interval becomes 10s
                 instead of 1 minute)
    1 = produce  normal behaviour; the only mode a release package may run in

The file is deliberately NOT part of a release package: ``build_release.ps1``
excludes it, so a shipped build carries no switch at all and reads "produce".
A missing, unreadable or unrecognised file is also "produce" - the safe
default must never depend on a file that may be absent.

The current mode is written into the assistant.log header, so every pasted
field log says which mode produced it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

MODE_FILE_NAME = "mode.txt"
DEBUG = 0
PRODUCE = 1

_MODE_NAMES = {DEBUG: "调试模式 (debug)", PRODUCE: "正式模式 (produce)"}


def mode_file_path(base: Optional[Path] = None) -> Path:
    """Return the mode file location (next to the application sources)."""

    root = Path(base) if base is not None else Path(__file__).resolve().parent
    return root / MODE_FILE_NAME


def read_mode(path: Optional[Path] = None) -> int:
    """Return ``DEBUG`` or ``PRODUCE``; anything unexpected is ``PRODUCE``."""

    target = Path(path) if path is not None else mode_file_path()
    try:
        text = target.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return PRODUCE
    except OSError:
        LOG.warning("mode file %s is unreadable; assuming produce mode", target)
        return PRODUCE
    if not text:
        return PRODUCE
    try:
        value = int(text.split()[0])
    except (IndexError, ValueError):
        LOG.warning(
            "mode file %s holds %r, not 0 or 1; assuming produce mode",
            target, text,
        )
        return PRODUCE
    if value not in _MODE_NAMES:
        LOG.warning(
            "mode file %s holds %r, not 0 or 1; assuming produce mode",
            target, text,
        )
        return PRODUCE
    return value


def debug_enabled(path: Optional[Path] = None) -> bool:
    """True only when mode.txt explicitly selects debug."""

    return read_mode(path) == DEBUG


def describe(mode: Optional[int] = None) -> str:
    """Short human label for the log/report."""

    value = read_mode() if mode is None else int(mode)
    return _MODE_NAMES.get(value, _MODE_NAMES[PRODUCE])


def log_run_mode(path: Optional[Path] = None) -> int:
    """Log the mode once at startup and return it."""

    mode = read_mode(path)
    target = Path(path) if path is not None else mode_file_path()
    if mode == DEBUG:
        LOG.warning(
            "RUN MODE: %s - temporary test minimums are active (%s)",
            describe(mode), target,
        )
    else:
        LOG.info("RUN MODE: %s (%s)", describe(mode), target)
    return mode
