"""Split user-owned settings from update-owned system calibration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Optional
from runtime_paths import application_root


USER_CONFIG_NAME = "user_config.json"
USER_CONFIG_VERSION_KEY = "user_config_updated_at"
AUTH_CONFIG_NAME = "auth.json"
AUTH_LOCAL_STATE_KEY = "_local"
SYSTEM_CONFIG_NAME = "system_config.json"
LEGACY_UNIFIED_NAME = "config.json"
_CONFIG_REPLACE_ATTEMPTS = 15
_CONFIG_REPLACE_RETRY_SECONDS = 0.20

LOG = logging.getLogger(__name__)

SECTION_FILES = {
    "minimap_calibration": "minimap_calibration.json",
    "recording": "recording-configuration.json",
    "rope_calibration": "rope_calibration.json",
    "drug": "drug_settings.json",
    "fixed_attack": "fixed_attack_settings.json",
    "additional_functions": "additional_functions_settings.json",
    "yolo_detection": "yolo_detection_settings.json",
    "ui_window": "ui_window_settings.json",
    "tracking_test": "tracking_test_settings.json",
    "license": "license_settings.json",
}

DEFAULT_USER_CONFIG: dict[str, Any] = {
    # Recording-owned, normalized border consumed independently by patrol.
    "minimap_calibration": {},
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
        # The six timed rows keep their historical field names so every saved
        # configuration stays valid.  Panel row -> owning worker:
        #   buff1 饲料   buff2 补药    (time drugs: one tap each)
        #   buff3 增益 1     buff4 增益 2    buff5 增益 3     buff6 增益 4
        #   (增益 rows: double tap, delivered through MotionArbiter)
        "hp_key": "delete", "mp_key": "end", "hp_threshold": 50,
        "mp_threshold": 30, "hp_enabled": True, "mp_enabled": True,
        "buff1_key": "home", "buff2_key": "insert", "buff3_key": "pageup",
        "buff4_key": "pagedown", "buff5_key": "-", "buff6_key": "-",
        "buff_interval_unit": "seconds",
        "buff1_interval": 600.0, "buff2_interval": 600.0,
        "buff3_interval": 600.0, "buff4_interval": 600.0,
        "buff5_interval": 600.0, "buff6_interval": 600.0,
        "buff1_enabled": False, "buff2_enabled": False,
        "buff3_enabled": False, "buff4_enabled": False,
        "buff5_enabled": False, "buff6_enabled": False,
    },
    "fixed_attack": {
        "attack_mode": "fixed", "min_interval_seconds": 3.0,
        "max_interval_seconds": 3.1, "attack_key": "ctrl",
        "attack_delivery": "stand",
        "jump_counterattack_enabled": False,
        "charge_min_seconds": 0.5, "charge_max_seconds": 0.5,
        "combo_attack_enabled": False,
        "combo_attack_slots": [
            {"min_count": 1, "max_count": 1, "key": "ctrl"},
            {"min_count": 1, "max_count": 1, "key": "ctrl"},
            {"min_count": 1, "max_count": 1, "key": "ctrl"},
        ],
        "random_jump_enabled": False,
        "random_jump_interval_seconds": 3.0,
        "random_jump_gap_seconds": .1,
        "stair_jump_enabled": True,
        "stationary_facing_direction": "right",
        "small_step_min_seconds": 5.0,
        "small_step_max_seconds": 5.0,
        # The post-attack quiet period inside 小碎步.  It is intentionally
        # separate from the interval between complete 小碎步 pairs.
        "small_step_post_attack_seconds": 0.70,
        "stationary_pickup_min_minutes": 15.0,
        "stationary_pickup_max_minutes": 15.0,
        "stationary_delayed_return_enabled": False,
        "dual_stationary_switch_min_minutes": 15.0,
        "dual_stationary_switch_max_minutes": 15.0,
    },
    "additional_functions": {
        "shutdown_enabled": False, "shutdown_hours": 3.0,
        "player_check_enabled": False, "disconnect_alert_enabled": False,
        "countdown_enabled": False, "countdown_interval_hours": 1.0,
        "lie_alert_enabled": False, "sound_alert_enabled": True,
        "screen_blink_enabled": False,
        "telegram_enabled": False, "telegram_bot_token": "",
        "telegram_chat_id": "", "telegram_machine_name": "",
        "quick_messages": [],
        # These controls live in the same Additional Functions panel.  Keep
        # them in the user schema so an older user_config is upgraded with
        # explicit values even before its operator changes either checkbox.
        "auto_lie_api_enabled": False,
        "auto_reconnect_enabled": False,
        "auto_reconnect_world": "蘑菇仔",
        "auto_reconnect_channel": 1,
        "reconnect_message": "",
        "player_request_message": "",
        "player_room_code": "",
        "player_channel_wait_minutes": 5.0,
    },
    "yolo_detection": {
        "threshold": .4, "attack_range": 30, "min_mob_size": 2,
        "detection_fps": 10, "zone_width": 60, "zone_height": 60,
        "zone_shift_y": 0, "show_detection": True, "auto_attack": False,
        "attack_key": "shift",
    },
    "ui_window": {},
    # 测试测谎 (tracking test): the recording the operator last replayed, so the
    # picker opens in the right folder instead of always falling back to the
    # bundled demo clip.
    "tracking_test": {"video": ""},
}

# Authorization is deliberately excluded from user_config.json.  Route
# recordings can therefore be exported/shared without exposing the operator's
# activation code, public equipment identity, memo, or validation history.
# ``auth.json`` is the signed local entitlement document; these values are its
# unsigned, local-only display state under ``_local``.
DEFAULT_AUTH_CONFIG: dict[str, Any] = {
    "activation_code": "",
    "memo": "",
    "equipment_id": "",
    "last_validation_valid": False,
    "last_validation_code": "",
    "last_validation_message": "",
    "last_validation_at": "",
}

DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "rope_calibration": {
        "map": "", "minimap_region": [0.0, .075, .12, .24],
        "rope_x": .4926, "calibrated_player_y": .65625,
        "horizontal_tolerance": .01, "horizontal_tolerance_diamonds": .12,
        "aligned_frames_required": 2, "climb_layer_confirm_frames": 2,
        "climb_layer_confirm_seconds": 0.0,
        "climb_arrival_world_tolerance": .2,
        "climb_world_y_change_required": .75,
        "climb_world_y_stall_change_required": .15,
        # Patrol repeats are now owned and persisted by each logical layer.
        # Keep the legacy fallback at one pass so an old, incomplete recording
        # can never silently run a layer twice.
        "climb_world_y_stall_frames": 2, "patrol_cycles_per_layer": 1,
        "potion_retry_attempts": 3, "potion_retry_delay_seconds": .05,
        "stair_jump_stall_frames": 7,
        "movement_hold_seconds": 2.0, "minimum_final_hold_seconds": .08,
        "minimum_movement_hold_seconds": .3,
        "estimated_minimap_speed": .11, "final_calculation_distance": .035,
        "final_calculation_diamonds": 1.025, "estimated_final_speed": .205,
        "final_move_safety_gain": .95, "climb_up_hold_seconds": .45,
        "climb_nudge_seconds": .20, "climb_y_change_required": .015,
        "climb_failed_shift_right_seconds": .1,
        "climb_attempt_interval_seconds": .5,
        "climb_failed_cycles_reset": 3,
        "climb_lateral_cycles_reset": 3,
        "drop_chord_hold_seconds": .1,
        # Only the recovery paths (reconnect / unresolved floor / dead flower)
        # still use this stall window; the plain patrol drop retries as soon as
        # the attempt is proven to have failed (DROP_RETRY_MIN_SECONDS).
        "drop_retry_seconds": 0.9,
        # Chord sent by the jump-up point: Alt is the game's jump key.  The
        # setting exists so a different in-game binding needs no rebuild.
        "jump_up_chord": "alt",
        "fall_detect_frames": 2, "fall_marker_y_gain": .015,
        "fall_settle_min_frames": 3, "fall_settle_epsilon": .15,
        "fall_settle_max_seconds": 1.2,
        "world_drift_check_interval_seconds": 2.0,
        "world_drift_reanchor_threshold": .35,
        "attack_block_max_seconds": 4.0,
        "near_rope_seconds": .5, "near_rope_range": .025,
        "near_rope_inner_range": .018, "near_rope_outer_range": .0229,
        "near_rope_diamonds": .66,
        "rope_approach_creep_seconds": .25,
        "rope_tiny_step_min_seconds": .05,
        "rope_tiny_step_max_seconds": .15,
        "stair_jump_enabled": True, "stair_jump_stall_diamonds": .25,
        "patrol_start_grace_seconds": 3.0,
        "stair_jump_attempts_max": 1, "stair_jump_grace_seconds": .8,
        "stair_jump_alt_hold_seconds": .06,
        "stair_jump_lead_seconds": .15,
        "stair_jump_climb_arrival_grace_seconds": 2.0,
        "other_player_check_interval_seconds": 0.0,
        # 8 frames is 1.6 s at the 5 fps capture cadence: the operator shortened
        # the stuck window from 20 frames after the rescue probe proved a real
        # stall is never acted on until Left/Right have failed to move the marker.
        "rescue_check_interval_seconds": 300.0, "rescue_stuck_frames": 8,
        "rescue_cycle_limit": 3,
        "rescue_probe_attack_block_seconds": 2.0,
        "rescue_probe_settle_seconds": .2,
        "rescue_probe_move_threshold": .006,
        "patrol_enabled": False,
    },
}

USER_SECTIONS = frozenset(DEFAULT_USER_CONFIG)
SYSTEM_SECTIONS = frozenset(DEFAULT_SYSTEM_CONFIG)
AUTH_SECTIONS = frozenset({"license"})
DEFAULT_CONFIG: dict[str, Any] = {
    **deepcopy(DEFAULT_USER_CONFIG), **deepcopy(DEFAULT_SYSTEM_CONFIG),
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _replace_with_retry(temporary: Path, destination: Path) -> None:
    """Atomically replace a file, tolerating short Windows sharing locks.

    Import/restart can briefly overlap the departing instance, Explorer's
    preview handler, or antivirus.  ``os.replace`` (used by ``Path.replace``)
    then raises WinError 5 even though the target becomes writable a moment
    later.  Retrying keeps atomic replacement while avoiding a startup crash.
    """

    last_error: Optional[PermissionError] = None
    for attempt in range(_CONFIG_REPLACE_ATTEMPTS):
        try:
            temporary.replace(destination)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < _CONFIG_REPLACE_ATTEMPTS:
                time.sleep(_CONFIG_REPLACE_RETRY_SECONDS)
    assert last_error is not None
    raise last_error


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-process temporary avoids collisions with a previous instance that
    # may still be closing.  It is always cleaned after a failed replacement,
    # so the user never has to recover a visible ``user_config.json.tmp``.
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _user_content(data: dict[str, Any]) -> dict[str, Any]:
    """Return settings only, excluding the update/export version tag."""

    content = deepcopy(data)
    content.pop(USER_CONFIG_VERSION_KEY, None)
    return content


def _new_user_config_version() -> str:
    # Microseconds make consecutive UI edits inside the same second distinct.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def user_config_version(path: Path) -> str:
    """Read the content version tag; empty means a legacy/unversioned file."""

    value = _read_json(Path(path)).get(USER_CONFIG_VERSION_KEY, "")
    return str(value) if isinstance(value, str) else ""


def _write_user_json(path: Path, data: dict[str, Any], previous: dict[str, Any]) -> bool:
    """Write only changed user settings and renew their timestamp tag."""

    before = _user_content(previous)
    after = _user_content(data)
    previous_tag = user_config_version(path)
    if before == after and previous_tag:
        return False
    output = deepcopy(after)
    output[USER_CONFIG_VERSION_KEY] = _new_user_config_version()
    _write_json(path, output)
    return True


class ConfigStore:
    """Route sections to either persistent user or shipped system JSON."""

    def __init__(
        self,
        user_path: Path,
        *,
        system_path: Optional[Path] = None,
        auth_path: Optional[Path] = None,
        legacy_root: Optional[Path] = None,
        legacy_unified_path: Optional[Path] = None,
    ) -> None:
        self.user_path = Path(user_path)
        self.path = self.user_path  # compatibility for existing callers
        self.system_path = Path(
            system_path or self.user_path.with_name(SYSTEM_CONFIG_NAME)
        )
        self.auth_path = Path(
            auth_path or self.user_path.with_name(AUTH_CONFIG_NAME)
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
                current = data.get(section)
                if isinstance(current, dict):
                    # A section can come from an older release and therefore
                    # lack options added to its panel later.  Fill only those
                    # missing keys: existing user values and any forward-
                    # compatible unknown keys always remain untouched.
                    defaults = DEFAULT_USER_CONFIG[section]
                    for key, value in defaults.items():
                        if key not in current:
                            current[key] = deepcopy(value)
                            changed = True
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
            # Authorization used to be embedded in the shareable recording.
            # Its actual signed document is now auth.json; the import/export
            # boundaries independently scrub this legacy shape too.
            if "license" in data:
                del data["license"]
                changed = True
            if not user_config_version(self.user_path):
                changed = True
            if changed:
                try:
                    _write_user_json(self.user_path, data, _read_json(self.user_path))
                except PermissionError:
                    # A valid imported config can still be used this launch.
                    # Do not make the whole helper fail merely because a short
                    # external Windows lock outlived the bounded retry window.
                    LOG.warning(
                        "用户配置暂时被其他程序占用；已保留当前文件并继续启动，"
                        "下次保存会再次写入"
                    )

    def _document_for_section(self, section: str) -> dict[str, Any]:
        if section in AUTH_SECTIONS:
            return _read_json(self.auth_path)
        if section in SYSTEM_SECTIONS:
            document = _read_json(self.system_path)
            return document or deepcopy(DEFAULT_SYSTEM_CONFIG)
        return _read_json(self.user_path)

    def path_for_section(self, section: str) -> Path:
        if section in AUTH_SECTIONS:
            return self.auth_path
        return self.system_path if section in SYSTEM_SECTIONS else self.user_path

    def read_section(self, section: str) -> dict[str, Any]:
        with self._lock:
            data = self._document_for_section(section)
            if section in AUTH_SECTIONS:
                local = data.get(AUTH_LOCAL_STATE_KEY, {})
                return deepcopy(local) if isinstance(local, dict) else {}
            value = data.get(section, DEFAULT_CONFIG.get(section, {}))
            return deepcopy(value) if isinstance(value, dict) else {}

    def write_section(self, section: str, value: dict[str, Any]) -> None:
        if section in SYSTEM_SECTIONS:
            raise PermissionError(
                f"{section} is update-owned in {self.system_path.name}"
            )
        if section not in USER_SECTIONS and section not in AUTH_SECTIONS:
            raise KeyError(f"unknown configuration section: {section}")
        with self._lock:
            if section in AUTH_SECTIONS:
                document = _read_json(self.auth_path)
                document[AUTH_LOCAL_STATE_KEY] = deepcopy(value)
                _write_json(self.auth_path, document)
                return
            data = _read_json(self.user_path)
            previous = deepcopy(data)
            data[section] = deepcopy(value)
            _write_user_json(self.user_path, data, previous)

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


_stores: dict[tuple[Path, Path, Path], ConfigStore] = {}
_stores_lock = threading.Lock()


def _default_user_root(package_root: Path) -> Path:
    """Find the persistent configuration folder for both launch paths.

    The installer launches from ``<install>\\助手配置`` through an
    environment variable.  A manual overlay update can also start the app
    executable directly, where that variable is absent; in that case retain
    the installed sibling folder instead of silently creating a new identity
    under ``app``.
    """

    configured = os.environ.get("MOONECLIPSE_CONFIG_DIR")
    if configured:
        return Path(configured)
    for candidate in (package_root / "助手配置", package_root.parent / "助手配置"):
        if candidate.is_dir():
            return candidate
    return package_root


def get_config_store(
    path: Optional[Path] = None,
    *,
    system_path: Optional[Path] = None,
) -> ConfigStore:
    # Match licensing's executable-aware root.  In a frozen build ``__file__``
    # can refer to an internal runtime directory; the launcher/config folder
    # is beside the executable instead.  A split root here would make the UI
    # write activation metadata somewhere other than licensing reads it.
    package_root = application_root(__file__)
    user_root = _default_user_root(package_root)
    requested = Path(path or user_root / USER_CONFIG_NAME).resolve()
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
    # Authorization is private runtime state at the package root.  It must
    # never sit beside the shareable ``助手配置\\user_config.json`` document.
    resolved_auth = (package_root / AUTH_CONFIG_NAME).resolve()
    key = (user_path, resolved_system, resolved_auth)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = ConfigStore(
                user_path,
                system_path=resolved_system,
                auth_path=resolved_auth,
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
    "AUTH_CONFIG_NAME", "AUTH_LOCAL_STATE_KEY", "AUTH_SECTIONS", "ConfigSectionFile", "ConfigStore", "DEFAULT_AUTH_CONFIG", "DEFAULT_CONFIG",
    "DEFAULT_SYSTEM_CONFIG", "DEFAULT_USER_CONFIG", "LEGACY_UNIFIED_NAME",
    "SECTION_FILES", "SYSTEM_CONFIG_NAME", "SYSTEM_SECTIONS",
    "USER_CONFIG_NAME", "USER_CONFIG_VERSION_KEY", "USER_SECTIONS",
    "config_section_file",
    "get_config_store",
    "user_config_version",
]
