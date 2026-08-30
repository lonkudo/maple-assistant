"""Split user-owned settings from update-owned system calibration."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
from typing import Any, Optional


USER_CONFIG_NAME = "user_config.json"
SYSTEM_CONFIG_NAME = "system_config.json"
LEGACY_UNIFIED_NAME = "config.json"

SECTION_FILES = {
    "recording": "recording-configuration.json",
    "rope_calibration": "rope_calibration.json",
    "drug": "drug_settings.json",
    "fixed_attack": "fixed_attack_settings.json",
    "additional_functions": "additional_functions_settings.json",
    "yolo_detection": "yolo_detection_settings.json",
    "ui_window": "ui_window_settings.json",
}

DEFAULT_USER_CONFIG: dict[str, Any] = {
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
    "drug": {
        "hp_key": "delete", "mp_key": "end", "hp_threshold": 50,
        "mp_threshold": 30, "hp_enabled": True, "mp_enabled": True,
        "buff1_key": "home", "buff2_key": "insert",
        "buff1_interval": 10.0, "buff2_interval": 10.0,
        "buff1_enabled": False, "buff2_enabled": False,
    },
    "fixed_attack": {
        "attack_mode": "fixed", "interval_seconds": 3.0,
        "random_gap_seconds": .1, "attack_key": "ctrl",
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

DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
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
        "potion_retry_attempts": 3, "potion_retry_delay_seconds": .05,
        "stair_jump_stall_frames": 10,
        "movement_hold_seconds": 2.0, "minimum_final_hold_seconds": .08,
        "minimum_movement_hold_seconds": .3,
        "estimated_minimap_speed": .11, "final_calculation_distance": .035,
        "final_calculation_diamonds": 1.025, "estimated_final_speed": .205,
        "final_move_safety_gain": .95, "climb_up_hold_seconds": .45,
        "climb_nudge_seconds": .1, "climb_y_change_required": .015,
        "climb_failed_shift_right_seconds": .1,
        "climb_attempt_interval_seconds": 1.0,
        "climb_failed_cycles_reset": 3,
        "drop_chord_hold_seconds": .1, "drop_retry_seconds": 1.5,
        "fall_detect_frames": 3, "fall_marker_y_gain": .015,
        "attack_block_max_seconds": 4.0,
        "near_rope_seconds": .5, "near_rope_range": .025,
        "near_rope_inner_range": .018, "near_rope_outer_range": .0229,
        "near_rope_diamonds": .66,
        "rope_approach_creep_seconds": .25,
        "rope_tiny_step_min_seconds": .05,
        "rope_tiny_step_max_seconds": .15,
        "stair_jump_enabled": True, "stair_jump_stall_diamonds": .25,
        "patrol_start_grace_seconds": 3.0,
        "stair_jump_attempts_max": 3, "stair_jump_grace_seconds": .8,
        "stair_jump_alt_hold_seconds": .06,
        "stair_jump_lead_seconds": .15,
        "stair_jump_climb_arrival_grace_seconds": 2.0,
        "other_player_check_interval_seconds": 0.0,
        "rescue_check_interval_seconds": 300.0, "rescue_stuck_frames": 20,
        "patrol_enabled": False,
    },
}

USER_SECTIONS = frozenset(DEFAULT_USER_CONFIG)
SYSTEM_SECTIONS = frozenset(DEFAULT_SYSTEM_CONFIG)
DEFAULT_CONFIG: dict[str, Any] = {
    **deepcopy(DEFAULT_USER_CONFIG), **deepcopy(DEFAULT_SYSTEM_CONFIG),
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class ConfigStore:
    """Route sections to either persistent user or shipped system JSON."""

    def __init__(
        self,
        user_path: Path,
        *,
        system_path: Optional[Path] = None,
        legacy_root: Optional[Path] = None,
        legacy_unified_path: Optional[Path] = None,
    ) -> None:
        self.user_path = Path(user_path)
        self.path = self.user_path  # compatibility for existing callers
        self.system_path = Path(
            system_path or self.user_path.with_name(SYSTEM_CONFIG_NAME)
        )
        self.legacy_root = Path(legacy_root or self.user_path.parent)
        self.legacy_unified_path = Path(
            legacy_unified_path
            or self.user_path.with_name(LEGACY_UNIFIED_NAME)
        )
        self._lock = threading.RLock()
        self._ensure_user_exists()

    def _ensure_user_exists(self) -> None:
        """Create/migrate only user-owned sections; never copy system tuning."""

        with self._lock:
            existed = self.user_path.is_file()
            data = _read_json(self.user_path) if existed else {}
            legacy_unified = _read_json(self.legacy_unified_path)
            changed = not existed
            for section in USER_SECTIONS:
                if isinstance(data.get(section), dict):
                    continue
                value = legacy_unified.get(section)
                if not isinstance(value, dict):
                    legacy = self.legacy_root / SECTION_FILES[section]
                    value = _read_json(legacy)
                if not isinstance(value, dict) or not value:
                    value = deepcopy(DEFAULT_USER_CONFIG[section])
                data[section] = deepcopy(value)
                changed = True
            # A user file must never retain update-owned sections, even if an
            # early development build accidentally wrote one there.
            for section in SYSTEM_SECTIONS:
                if section in data:
                    del data[section]
                    changed = True
            if changed:
                _write_json(self.user_path, data)

    def _document_for_section(self, section: str) -> dict[str, Any]:
        if section in SYSTEM_SECTIONS:
            document = _read_json(self.system_path)
            return document or deepcopy(DEFAULT_SYSTEM_CONFIG)
        return _read_json(self.user_path)

    def path_for_section(self, section: str) -> Path:
        return self.system_path if section in SYSTEM_SECTIONS else self.user_path

    def read_section(self, section: str) -> dict[str, Any]:
        with self._lock:
            data = self._document_for_section(section)
            value = data.get(section, DEFAULT_CONFIG.get(section, {}))
            return deepcopy(value) if isinstance(value, dict) else {}

    def write_section(self, section: str, value: dict[str, Any]) -> None:
        if section in SYSTEM_SECTIONS:
            raise PermissionError(
                f"{section} is update-owned in {self.system_path.name}"
            )
        if section not in USER_SECTIONS:
            raise KeyError(f"unknown configuration section: {section}")
        with self._lock:
            data = _read_json(self.user_path)
            data[section] = deepcopy(value)
            _write_json(self.user_path, data)


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
        return f"{self.store.path_for_section(self.section)}[{self.section}]"


_stores: dict[tuple[Path, Path], ConfigStore] = {}
_stores_lock = threading.Lock()


def get_config_store(
    path: Optional[Path] = None,
    *,
    system_path: Optional[Path] = None,
) -> ConfigStore:
    package_root = Path(__file__).resolve().parent
    requested = Path(path or package_root / USER_CONFIG_NAME).resolve()
    # Preserve --config config.json compatibility while migrating writes to
    # user_config.json. The old unified file remains untouched as a backup.
    if requested.name.casefold() == LEGACY_UNIFIED_NAME:
        legacy_unified = requested
        user_path = requested.with_name(USER_CONFIG_NAME)
    else:
        user_path = requested
        legacy_unified = requested.with_name(LEGACY_UNIFIED_NAME)
    resolved_system = Path(
        system_path or package_root / SYSTEM_CONFIG_NAME
    ).resolve()
    key = (user_path, resolved_system)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = ConfigStore(
                user_path,
                system_path=resolved_system,
                legacy_root=requested.parent,
                legacy_unified_path=legacy_unified,
            )
            _stores[key] = store
        return store


def config_section_file(
    section: str, path: Optional[Path] = None
) -> ConfigSectionFile:
    return ConfigSectionFile(get_config_store(path), section)


__all__ = [
    "ConfigSectionFile", "ConfigStore", "DEFAULT_CONFIG",
    "DEFAULT_SYSTEM_CONFIG", "DEFAULT_USER_CONFIG", "LEGACY_UNIFIED_NAME",
    "SECTION_FILES", "SYSTEM_CONFIG_NAME", "SYSTEM_SECTIONS",
    "USER_CONFIG_NAME", "USER_SECTIONS", "config_section_file",
    "get_config_store",
]
