"""Four-digit Maple Assistant release version handling."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


VERSION_PATTERN = re.compile(r"\d{4}")
FIRST_VERSION = "0000"
LAST_VERSION = "9999"


def parse_version(value: str) -> str:
    """Validate and return one zero-padded four-digit version."""

    text = str(value).strip()
    if VERSION_PATTERN.fullmatch(text) is None:
        raise ValueError("version must contain exactly four digits (0000-9999)")
    return text


def read_version(path: Path | None = None) -> str:
    """Read the current version, falling back to 0000 in a source checkout."""

    version_path = Path(path or Path(__file__).with_name("VERSION"))
    try:
        return parse_version(version_path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return FIRST_VERSION


def next_version(path: Path) -> str:
    """Return the next release number; a missing file starts at 0000."""

    version_path = Path(path)
    if not version_path.is_file():
        return FIRST_VERSION
    current = parse_version(version_path.read_text(encoding="ascii"))
    number = int(current)
    if number >= int(LAST_VERSION):
        raise OverflowError("release version 9999 is the maximum")
    return f"{number + 1:04d}"


def version_label(path: Path | None = None) -> str:
    """UI-ready version marker."""

    return f"v{read_version(path)}"


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("current", "next"))
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path(__file__).with_name("VERSION"))
    args = parser.parse_args()
    try:
        value = (read_version(args.path) if args.command == "current"
                 else next_version(args.path))
    except (OSError, ValueError, OverflowError) as exc:
        parser.error(str(exc))
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "FIRST_VERSION", "LAST_VERSION", "next_version", "parse_version",
    "read_version", "version_label",
]
