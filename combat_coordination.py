"""Cross-process coordination between the patrol and attack workers.

The patrol worker (``MovementWorker``, assistant process) and the attack
worker (YOLO subprocess) run in *different processes*, so they cannot share
threading events.  They coordinate through a small JSON state file::

    attack_state.json:
        {"active": true|false, "ts": epoch_seconds, "target": [x, y]|null}

The attack worker writes the file every frame; the patrol worker reads it
and holds position while an attack target is active.  This gives the
attack logic priority over patrol movement without coupling the two
workers together.

The file is written atomically (temp file + rename) so a reader never
sees a half-written payload.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple


class AttackStateFile:
    """Read/write the shared attack-state JSON."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def write(
        self,
        active: bool,
        target: Optional[Tuple[float, float]] = None,
    ) -> None:
        """Persist the current attack state (atomic temp+rename)."""

        data = {
            "active": bool(active),
            "ts": time.time(),
            "target": list(target) if target is not None else None,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self) -> Optional[dict]:
        """Return the raw state dict, or None when unreadable/missing."""

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_active(self, max_age: float = 1.0) -> bool:
        """True when the attack worker currently targets a mob.

        A missing file, a stale file (older than ``max_age`` seconds, e.g.
        the YOLO subprocess died), or ``active: false`` all mean "not
        attacking" so patrol resumes.
        """

        data = self.read()
        if not data or not data.get("active"):
            return False
        age = time.time() - float(data.get("ts", 0.0))
        return 0.0 <= age <= max_age

    def target(self) -> Optional[Tuple[float, float]]:
        """Return the currently reported target center, if any."""

        data = self.read()
        if not data or not data.get("active"):
            return None
        raw = data.get("target")
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            return None
        return (float(raw[0]), float(raw[1]))


class PatrolStateFile:
    """Read/write the shared patrol-state JSON.

    The patrol worker publishes whether the character is busy climbing or
    dropping; the YOLO attack worker reads it and blocks attacks while the
    character is on a rope (attacking would fight the climb).
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def write(self, busy: bool, action: Optional[str] = None) -> None:
        """Persist the current patrol state (atomic temp+rename)."""

        data = {
            "busy": bool(busy),
            "action": action,
            "ts": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_busy(self, max_age: float = 1.0) -> bool:
        """True when patrol is climbing/dropping (recent + busy)."""

        data = self.read()
        if not data or not data.get("busy"):
            return False
        age = time.time() - float(data.get("ts", 0.0))
        return 0.0 <= age <= max_age


__all__ = ["AttackStateFile", "RopeStateFile", "PatrolStateFile"]


class RopeStateFile:
    """Read/write the shared YOLO rope-state JSON.

    The YOLO subprocess publishes the rope's screen position (and the
    character's screen X) every frame; the patrol worker consumes it to
    gate the inner-gap jump on the real screen gap instead of the coarse
    minimap estimate.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def write(
        self,
        visible: bool,
        rope_x: Optional[float] = None,
        rope_y: Optional[float] = None,
        char_x: Optional[float] = None,
        char_y: Optional[float] = None,
    ) -> None:
        """Persist the current rope state (atomic temp+rename)."""

        data = {
            "visible": bool(visible),
            "ts": time.time(),
            "rope_x": float(rope_x) if rope_x is not None else None,
            "rope_y": float(rope_y) if rope_y is not None else None,
            "char_x": float(char_x) if char_x is not None else None,
            "char_y": float(char_y) if char_y is not None else None,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self) -> Optional[dict]:
        """Return the raw state dict, or None when unreadable/missing."""

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_fresh(self, max_age: float = 0.6) -> bool:
        """True when the subprocess reported within the last ``max_age`` s."""

        data = self.read()
        if not data:
            return False
        age = time.time() - float(data.get("ts", 0.0))
        return 0.0 <= age <= max_age

    def screen_gap(self) -> Optional[float]:
        """Return rope_x - char_x (positive = rope to the right), or None."""

        data = self.read()
        if not data or not data.get("visible"):
            return None
        rx = data.get("rope_x")
        cx = data.get("char_x")
        if rx is None or cx is None:
            return None
        return float(rx) - float(cx)
