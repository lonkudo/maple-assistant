"""Single-file configuration store with one-time legacy JSON migration."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
from typing import Any, Optional


SECTION_FILES = {
    "recording": "recording-configuration.json",
    "rope_calibration": "rope_calibration.json",
    "drug": "drug_settings.json",
    "fixed_attack": "fixed_attack_settings.json",
    "additional_functions": "additional_functions_settings.json",
    "yolo_detection": "yolo_detection_settings.json",
    "ui_window": "ui_window_settings.json",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "recording": {
        "configuration_id": "patrol_recording", "map_name": "",
        "patrol_enabled": False, "climbing_enabled": True,
        "route_order": [], "final_layer_action": "drop_to_first_layer",
        "first_layer": "layer1", "patrol_start_layer": "",
        "patrol_end_layer": "", "minimap_region": [0.0, .075, .12, .24],
        "rope": {"x": .5, "near_range": .025, "inner_range": .018,
                 "outer_range": .0229},
        "layers": {"layer1": {"y_tolerance": .02,
                                "calibration_status":
                                "awaiting_left_rope_right"}},
    },
    "rope_calibration": {
        "map": "", "minimap_region": [0.0, .075, .12, .24],
        "rope_x": .4926, "calibrated_player_y": .65625,
        "horizontal_tolerance": .01, "horizontal_tolerance_diamonds": .293,
        "aligned_frames_required": 2, "climb_layer_confirm_frames": 3,
        "climb_layer_confirm_seconds": .3,
        "climb_arrival_world_tolerance": .2,
        "climb_world_y_change_required": .75,
        "climb_world_y_stall_change_required": .15,
        "climb_world_y_stall_frames": 2, "patrol_cycles_per_layer": 2,
        "movement_hold_seconds": 2.0, "minimum_final_hold_seconds": .08,
        "minimum_movement_hold_seconds": .3,
        "estimated_minimap_speed": .11, "final_calculation_distance": .035,
        "final_calculation_diamonds": 1.025, "estimated_final_speed": .205,
        "final_move_safety_gain": .95, "climb_up_hold_seconds": .45,
        "climb_nudge_seconds": .1, "climb_y_change_required": .015,
        "climb_failed_shift_right_seconds": .1,
        "drop_chord_hold_seconds": .1, "drop_retry_seconds": 1.5,
        "near_rope_seconds": .5, "near_rope_range": .025,
        "near_rope_inner_range": .018, "near_rope_outer_range": .0229,
        "near_rope_diamonds": .66, "patrol_enabled": False,
    },
    "drug": {
        "hp_key": "delete", "mp_key": "end", "hp_threshold": 50,
        "mp_threshold": 30, "hp_enabled": True, "mp_enabled": True,
        "buff1_key": "home", "buff2_key": "insert",
        "buff1_interval": 10.0, "buff2_interval": 10.0,
        "buff1_enabled": False, "buff2_enabled": False,
    },
    "fixed_attack": {
        "attack_mode": "yolo", "interval_seconds": 3.0,
        "attack_key": "ctrl",
    },
    "additional_functions": {
        "shutdown_enabled": False, "shutdown_hours": 3.0,
        "player_check_enabled": False, "disconnect_alert_enabled": False,
        "countdown_enabled": False, "countdown_interval_hours": 1.0,
        "lie_alert_enabled": False, "screen_blink_enabled": False,
    },
    "yolo_detection": {
        "threshold": .4, "attack_range": 30, "min_mob_size": 2,
        "detection_fps": 10, "zone_width": 60, "zone_height": 60,
        "zone_shift_y": 0, "show_detection": True, "auto_attack": False,
        "attack_key": "shift",
    },
    "ui_window": {},
}


class ConfigStore:
    def __init__(self, path: Path, *, legacy_root: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.legacy_root = Path(legacy_root or self.path.parent)
        self._lock = threading.RLock()
        self._ensure_exists()

    def _read_document(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write_document(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _ensure_exists(self) -> None:
        with self._lock:
            existed = self.path.is_file()
            data = self._read_document() if existed else deepcopy(DEFAULT_CONFIG)
            changed = not existed
            for section, filename in SECTION_FILES.items():
                if existed and section in data:
                    continue
                legacy = self.legacy_root / filename
                try:
                    value = json.loads(legacy.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    value = deepcopy(DEFAULT_CONFIG[section])
                if isinstance(value, dict):
                    data[section] = value
                    changed = True
            if changed:
                self._write_document(data)

    def read_section(self, section: str) -> dict[str, Any]:
        with self._lock:
            data = self._read_document()
            value = data.get(section, DEFAULT_CONFIG.get(section, {}))
            return deepcopy(value) if isinstance(value, dict) else {}

    def write_section(self, section: str, value: dict[str, Any]) -> None:
        with self._lock:
            data = self._read_document()
            data[section] = deepcopy(value)
            self._write_document(data)


class ConfigSectionFile:
    """Small read_text/write_text adapter for existing JSON UI helpers."""

    def __init__(self, store: ConfigStore, section: str) -> None:
        self.store = store
        self.section = section

    def read_text(self, encoding: str = "utf-8", **_kwargs: Any) -> str:
        del encoding
        return json.dumps(self.store.read_section(self.section), ensure_ascii=False)

    def write_text(self, text: str, encoding: str = "utf-8", **_kwargs: Any) -> int:
        del encoding
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("configuration section must be a JSON object")
        self.store.write_section(self.section, value)
        return len(text)

    def __str__(self) -> str:
        return f"{self.store.path}[{self.section}]"


_stores: dict[Path, ConfigStore] = {}
_stores_lock = threading.Lock()


def get_config_store(path: Optional[Path] = None) -> ConfigStore:
    resolved = Path(path or Path(__file__).with_name("config.json")).resolve()
    with _stores_lock:
        store = _stores.get(resolved)
        if store is None:
            store = ConfigStore(resolved)
            _stores[resolved] = store
        return store


def config_section_file(section: str, path: Optional[Path] = None) -> ConfigSectionFile:
    return ConfigSectionFile(get_config_store(path), section)


__all__ = [
    "ConfigSectionFile", "ConfigStore", "DEFAULT_CONFIG", "SECTION_FILES",
    "config_section_file", "get_config_store",
]
