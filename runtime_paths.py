"""Stable paths for source and Nuitka standalone executions."""

from __future__ import annotations

from pathlib import Path
import sys


def application_root(reference_file: str | None = None) -> Path:
    """Return the install folder, never Nuitka's internal module location."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(reference_file or __file__).resolve().parent


def package_format(root: Path) -> str:
    """Return the update format that can safely replace this installation."""

    root = Path(root)
    if any((root / name).is_file() for name in ("TodoHelper.exe", "MapleAssistant.exe")):
        return "frozen"
    return "source"


__all__ = ["application_root", "package_format"]
