"""Semantic three-part MapleAssistant release version handling (X.Y.Z)."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
FIRST_VERSION = "1.0.0"


def parse_version(value: str) -> str:
    """Validate and return one ``MAJOR.MINOR.PATCH`` version."""

    text = str(value).strip()
    if VERSION_PATTERN.fullmatch(text) is None:
        raise ValueError("version must be three numbers like 1.0.0")
    return text


def version_key(value: str) -> tuple[int, int, int]:
    """Sort key for one version; anything unreadable sorts lowest.

    ``1.0.10`` must sort after ``1.0.9``, which a plain string or ``int()`` comparison gets wrong.
    """

    match = VERSION_PATTERN.fullmatch(str(value).strip())
    if match is None:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def read_version(path: Path | None = None) -> str:
    """Read the current version, falling back to the first release in a source checkout."""

    version_path = Path(path or Path(__file__).with_name("VERSION"))
    try:
        return parse_version(version_path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return FIRST_VERSION


def next_version(path: Path, *, part: str = "patch") -> str:
    """Return the next release version; a missing file starts at the first release.

    ``part`` is ``"patch"`` by default - the operator's rule is that the LAST number is increased
    unless he says otherwise - and ``"minor"`` / ``"major"`` for the two explicit cases.
    """

    if part not in ("patch", "minor", "major"):
        raise ValueError("part must be patch, minor or major")
    version_path = Path(path)
    if not version_path.is_file():
        return FIRST_VERSION
    current = parse_version(version_path.read_text(encoding="ascii"))
    major, minor, patch = (int(value) for value in current.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def version_label(path: Path | None = None) -> str:
    """UI-ready version marker."""

    return f"v{read_version(path)}"


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("current", "next"))
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path(__file__).with_name("VERSION"))
    parser.add_argument("--major", action="store_true", help="increase the FIRST number")
    parser.add_argument("--minor", action="store_true", help="increase the SECOND number")
    # --patch is the default; it is accepted explicitly so callers (release_now.ps1) can always
    # name the part they want instead of relying on an omitted flag.
    parser.add_argument("--patch", action="store_true", help="increase the LAST number (default)")
    args = parser.parse_args()
    part = "major" if args.major else ("minor" if args.minor else "patch")
    try:
        value = (read_version(args.path) if args.command == "current"
                 else next_version(args.path, part=part))
    except (OSError, ValueError, OverflowError) as exc:
        parser.error(str(exc))
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "FIRST_VERSION", "next_version", "parse_version",
    "read_version", "version_key", "version_label",
]
