"""Persistent state and countdown logic for the standalone BOSS Tracker."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable
from uuid import uuid4


DEFAULT_INTERVAL_MINUTES = 60.0
MIN_INTERVAL_MINUTES = 0.1
MAX_INTERVAL_MINUTES = 10080.0


def _clean_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clean_minutes(value: Any) -> float:
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    if not math.isfinite(minutes):
        return DEFAULT_INTERVAL_MINUTES
    return min(MAX_INTERVAL_MINUTES, max(MIN_INTERVAL_MINUTES, minutes))


class BossTrackerModel:
    """Own channel deadlines, statistics, and atomic persistence."""

    def __init__(
        self,
        config_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_path = Path(config_path)
        self._clock = clock
        self._lock = threading.RLock()
        self._data = self._load()

    def _default_data(self) -> dict[str, Any]:
        return {
            "version": 2,
            "universal_interval_minutes": DEFAULT_INTERVAL_MINUTES,
            "channels": [],
            "statistics": {"boss_kills": 0, "custom": []},
            "window_geometry": "",
        }

    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] | None = None
        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Keep the application usable; the next successful edit repairs
            # the malformed file with normalized data.
            pass
        return self._normalize(data if data is not None else self._default_data())

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        now = self._clock()
        if "universal_interval_minutes" in data:
            interval_minutes = _clean_minutes(data["universal_interval_minutes"])
        else:
            # Preserve values written by every pre-minute release.
            try:
                interval_minutes = _clean_minutes(
                    float(data.get("universal_interval_hours", 1.0)) * 60.0
                )
            except (TypeError, ValueError):
                interval_minutes = DEFAULT_INTERVAL_MINUTES
        interval_seconds = interval_minutes * 60.0

        channels: list[dict[str, Any]] = []
        raw_channels = data.get("channels", [])
        if isinstance(raw_channels, list):
            for index, raw in enumerate(raw_channels, start=1):
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name", "")).strip() or f"频道 {index}"
                try:
                    deadline = float(raw.get("deadline", now + interval_seconds))
                except (TypeError, ValueError):
                    deadline = now + interval_seconds
                if not math.isfinite(deadline):
                    deadline = now + interval_seconds
                channels.append({
                    "id": str(raw.get("id") or uuid4().hex),
                    "name": name,
                    "deadline": deadline,
                })

        statistics = data.get("statistics", {})
        if not isinstance(statistics, dict):
            statistics = {}
        custom: list[dict[str, Any]] = []
        raw_custom = statistics.get("custom", [])
        if isinstance(raw_custom, list):
            for index, raw in enumerate(raw_custom, start=1):
                if not isinstance(raw, dict):
                    continue
                custom.append({
                    "id": str(raw.get("id") or uuid4().hex),
                    "name": str(raw.get("name", "")).strip()
                    or f"自定义项目 {index}",
                    "count": _clean_count(raw.get("count")),
                })

        return {
            "version": 2,
            "universal_interval_minutes": interval_minutes,
            "channels": channels,
            "statistics": {
                "boss_kills": _clean_count(statistics.get("boss_kills")),
                "custom": custom,
            },
            "window_geometry": str(data.get("window_geometry", "")),
        }

    def _save_locked(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self._data, ensure_ascii=False, indent=2, sort_keys=False
        ) + "\n"
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=self.config_path.parent,
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
            temporary.replace(self.config_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    @property
    def interval_seconds(self) -> float:
        with self._lock:
            return float(self._data["universal_interval_minutes"]) * 60.0

    def set_interval_minutes(self, minutes: float) -> float:
        """Change the shared gap and reset every channel to the full gap."""

        minutes = _clean_minutes(minutes)
        deadline = self._clock() + minutes * 60.0
        with self._lock:
            self._data["universal_interval_minutes"] = minutes
            for channel in self._data["channels"]:
                channel["deadline"] = deadline
            self._save_locked()
        return minutes

    def add_channel(self, name: str) -> str:
        with self._lock:
            raw_name = str(name).strip()
            if (
                not raw_name.isdigit()
                or raw_name.startswith("0")
                or not 1 <= int(raw_name) <= 60
            ):
                raise ValueError("channel number must be an integer from 1 to 60")
            clean_name = str(int(raw_name))
            if any(
                channel["name"] == clean_name
                for channel in self._data["channels"]
            ):
                raise ValueError("channel number already exists")
            channel_id = uuid4().hex
            self._data["channels"].append({
                "id": channel_id,
                "name": clean_name,
                "deadline": self._clock() + self.interval_seconds,
            })
            self._save_locked()
            return channel_id

    def delete_channel(self, channel_id: str) -> bool:
        with self._lock:
            before = len(self._data["channels"])
            self._data["channels"] = [
                row for row in self._data["channels"]
                if row["id"] != channel_id
            ]
            changed = len(self._data["channels"]) != before
            if changed:
                self._save_locked()
            return changed

    def reset_channel(self, channel_id: str) -> bool:
        with self._lock:
            for channel in self._data["channels"]:
                if channel["id"] == channel_id:
                    channel["deadline"] = self._clock() + self.interval_seconds
                    self._save_locked()
                    return True
            return False

    def set_channel_remaining(self, channel_id: str, seconds: float) -> bool:
        """Move one channel deadline within the universal interval."""

        with self._lock:
            remaining = max(0.0, min(float(seconds), self.interval_seconds))
            for channel in self._data["channels"]:
                if channel["id"] == channel_id:
                    channel["deadline"] = self._clock() + remaining
                    self._save_locked()
                    return True
            return False

    def channel_status(self) -> list[dict[str, Any]]:
        """Return display rows without mutating expired deadlines."""

        now = self._clock()
        interval = self.interval_seconds
        with self._lock:
            return [
                {
                    **deepcopy(channel),
                    "remaining": max(
                        0.0, min(interval, float(channel["deadline"]) - now)
                    ),
                    "interval": interval,
                }
                for channel in self._data["channels"]
            ]

    def advance_expired(self) -> list[str]:
        """Reset expired channels independently and return their names."""

        now = self._clock()
        interval = self.interval_seconds
        expired: list[str] = []
        with self._lock:
            for channel in self._data["channels"]:
                deadline = float(channel["deadline"])
                if deadline > now:
                    continue
                expired.append(channel["name"])
                missed = math.floor((now - deadline) / interval) + 1
                channel["deadline"] = deadline + missed * interval
            if expired:
                self._save_locked()
        return expired

    def change_boss_kills(self, delta: int) -> int:
        with self._lock:
            stats = self._data["statistics"]
            stats["boss_kills"] = max(0, stats["boss_kills"] + int(delta))
            self._save_locked()
            return stats["boss_kills"]

    def add_custom_stat(self, name: str = "") -> str:
        with self._lock:
            item_id = uuid4().hex
            clean_name = str(name).strip() or (
                f"自定义项目 {len(self._data['statistics']['custom']) + 1}"
            )
            self._data["statistics"]["custom"].append({
                "id": item_id,
                "name": clean_name,
                "count": 0,
            })
            self._save_locked()
            return item_id

    def rename_custom_stat(self, item_id: str, name: str) -> bool:
        with self._lock:
            for item in self._data["statistics"]["custom"]:
                if item["id"] == item_id:
                    item["name"] = str(name).strip() or "未命名项目"
                    self._save_locked()
                    return True
            return False

    def change_custom_stat(self, item_id: str, delta: int) -> int | None:
        with self._lock:
            for item in self._data["statistics"]["custom"]:
                if item["id"] == item_id:
                    item["count"] = max(0, item["count"] + int(delta))
                    self._save_locked()
                    return item["count"]
            return None

    def delete_custom_stat(self, item_id: str) -> bool:
        with self._lock:
            rows = self._data["statistics"]["custom"]
            before = len(rows)
            self._data["statistics"]["custom"] = [
                row for row in rows if row["id"] != item_id
            ]
            changed = len(self._data["statistics"]["custom"]) != before
            if changed:
                self._save_locked()
            return changed

    def clear_all_data(self) -> None:
        """Clear every channel and statistic while retaining app settings."""

        with self._lock:
            self._data["channels"] = []
            self._data["statistics"] = {"boss_kills": 0, "custom": []}
            self._save_locked()

    def set_window_geometry(self, geometry: str) -> None:
        with self._lock:
            self._data["window_geometry"] = str(geometry)
            self._save_locked()


__all__ = ["BossTrackerModel"]
