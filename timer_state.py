"""Small durable state file for resuming the independent reminder timer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def timer_state_path() -> Path:
    """Return the local runtime state path, separate from user settings."""

    return Path(__file__).with_name("timer.json")


def load_timer_state(path: Path) -> dict[str, Any]:
    """Return validated raw timer state, or an empty state when unavailable."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_timer_state(
    path: Path,
    *,
    enabled: bool,
    deadline_at: float | None,
    interval_seconds: float,
) -> None:
    """Atomically save a wall-clock deadline that survives process restart."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "enabled": bool(enabled),
        "deadline_at": float(deadline_at) if deadline_at is not None else None,
        "interval_seconds": max(0.01, float(interval_seconds)),
    }
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix="timer-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


__all__ = ["load_timer_state", "save_timer_state", "timer_state_path"]
