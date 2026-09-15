"""Per-run logs for the API work: connection log, detection log, summary, annotated frames.

One folder per run, so a single drill can be reviewed on its own:

    work/api_test/<name>_<stamp>/
        connection.log   human readable: every probe, the chosen port, handshake, errors
        detection.jsonl  one JSON object per frame (what we sent, what came back, where it mapped)
        summary.json     counts, quota before/after, timings, accuracy, paths
        frames/          annotated frames (ROI box, answered position), a few per run

Deliberately plain: no logging framework, so the file reads like a transcript of the drill and can
be sent as-is for diagnosis.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from image_io import save_screenshot

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "work" / "api_test"
ANNOTATED_QUALITY = 95


@dataclass
class RunLog:
    """One run's folder + writers.  ``RunLog()`` picks ``work/api_test/<name>_<stamp>/``."""

    name: str = "api_test"
    root: Path = DEFAULT_ROOT
    folder: Optional[Path] = None
    started_at: float = field(default_factory=time.time)
    records: list[dict] = field(default_factory=list)
    connection_lines: list[str] = field(default_factory=list)
    _connection: Any = None
    _detection: Any = None
    frames_saved: int = 0

    def __post_init__(self) -> None:
        if self.folder is None:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started_at))
            self.folder = Path(self.root) / f"{self.name}_{stamp}"
        self.folder = Path(self.folder)
        (self.folder / "frames").mkdir(parents=True, exist_ok=True)
        self._connection = (self.folder / "connection.log").open("w", encoding="utf-8")
        self._detection = (self.folder / "detection.jsonl").open("w", encoding="utf-8")
        self.connection(f"run started: {self.name}")

    # ------------------------------------------------------------------ writing
    def connection(self, message: str, **fields: Any) -> None:
        """One line in connection.log: an event plus any key=value detail."""

        stamp = time.strftime("%H:%M:%S", time.localtime())
        detail = "  ".join(f"{key}={value}" for key, value in fields.items())
        line = f"[{stamp}] {message}" + (f"  | {detail}" if detail else "")
        self.connection_lines.append(line)
        if self._connection is not None:
            self._connection.write(line + "\n")
            self._connection.flush()

    def detection(self, record: dict) -> None:
        """One frame's record in detection.jsonl (also kept in memory for the summary)."""

        entry = dict(record)
        entry.setdefault("t", round(time.time() - self.started_at, 3))
        self.records.append(entry)
        if self._detection is not None:
            self._detection.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._detection.flush()

    def save_frame(self, image, index: int) -> Optional[Path]:
        """An annotated frame in frames/ -> its path."""

        path = self.folder / "frames" / f"frame_{index:04d}.jpg"
        saved = save_screenshot(path, image, quality=ANNOTATED_QUALITY)
        if saved is not None:
            self.frames_saved += 1
        return saved

    def summary(self, data: dict) -> Path:
        """Write summary.json and close the writers."""

        payload = dict(data)
        payload.setdefault("name", self.name)
        payload.setdefault("folder", str(self.folder))
        payload.setdefault("frames_logged", len(self.records))
        payload.setdefault("frames_saved", self.frames_saved)
        payload.setdefault("duration_sec", round(time.time() - self.started_at, 2))
        path = self.folder / "summary.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def close(self) -> None:
        for handle in (self._connection, self._detection):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._connection = None
        self._detection = None

    # ------------------------------------------------------------------ helpers
    @property
    def connection_path(self) -> Path:
        return self.folder / "connection.log"

    @property
    def detection_path(self) -> Path:
        return self.folder / "detection.jsonl"

    def describe(self) -> str:
        return str(self.folder)


__all__ = ["RunLog", "DEFAULT_ROOT"]
