# -*- coding: utf-8 -*-
"""Auto lie-pass worker (GPU fade-target tracker integration).

When the optional "自动过测谎" feature is enabled and this machine has the
package tracker dependencies, this worker reacts to a lie-event bell and
drives the real mouse onto the fading white target box.  Passing relies on
cursor presence only: **no click is ever sent** - the mouse movement itself
is driven by the tracker's own ``mouse_aim_controller`` module.

Bells (what starts a sequence):
- the existing LieDetectorWorker #c9ced0 pre-window slice event, and/or
- the optional novelty watcher: while armed and idle it cheaply watches
  live frames and rings when a compact white popup appears and persists,
  so the #c9ced0 rule is not strictly required.

Timeline of one event:
- The #c9ced0 slice (plus the dingdong alert) is only the ALARM.  It says
  "a lie event is happening"; it never ends anything.
- Cutie takes over the target TARGET_TAKEOVER_SECONDS after that alarm.
  From then on live frames are cropped to the lie popup (the "second
  window") and scanned for the bright-white target box (tracker's own
  white-popup rule: compact, square-ish, centre-biased), confirmed across a
  few frames, so Cutie is never seeded on the wrong object.
- Cutie then follows the target while it DIMS and drives the real mouse
  onto it.  Dimming (or the white mask disappearing) is the event itself,
  never an end signal.  While a sequence drives the cursor, every other
  in-process mouse controller (e.g. an open 测试测谎 demo window) is
  suspended and restored afterwards.

Design rules from the operator:
- the game window always regains focus: only mouse movement is required;
- no click: the server passes on cursor presence;
- a lie event happens once every few hours: never concurrent;
- if tracking is lost, leave the cursor where it is (no recovery motion).

This module intentionally imports only the standard library at module
level.  torch / cutie / cv2 / target_tracker are imported lazily inside
methods so the assistant starts normally on machines without the optional
tracker dependencies (probe_auto_lie_environment() reports availability to
the UI).
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

LOG = logging.getLogger(__name__)

# Assistant repository layout: this file lives beside target_tracker/.
_TARGET_TRACKER_DIR = Path(__file__).resolve().parent / "target_tracker"
_RELEASE_MANIFEST = Path(__file__).resolve().parent / "release_variant.json"

# End-of-sequence thresholds.
END_CONFIDENCE = 0.30          # below this the target is considered lost
LOW_CONF_END_SECONDS = 2.0     # how long a low-confidence run may last
STALE_END_SECONDS = 2.0        # no fresh live result for this long -> end
MAX_SEQUENCE_SECONDS = 45.0    # hard safety cap for one lie event
SMOOTH_ALPHA = 0.4             # light exponential smoothing on aim points
# Note: no click is ever sent.  Passing relies on cursor presence alone; the
# real mouse is driven by the tracker's own mouse_aim_controller module.

# A lie square must be at least this many pixels wide/tall to seed tracking.
_MIN_SEED_SIZE = 8

# A real lie event starts with the #c9ced0 pre-window slice: it is only the
# ALARM for the event (the assistant also plays dingdong.mp3 for it), not
# something that ends anything.  The actual lie popup - the "second window" -
# follows, and the tracker is only ever fed that popup, never the whole game
# client.  Cutie takes over the target TARGET_TAKEOVER_SECONDS after the
# alarm; AWAIT_TARGET_SECONDS is the extra grace used to find the white
# target from then on, _MIN_TARGET_SIZE the smallest accepted blob, and a
# candidate must stay stable across _CANDIDATE_STABLE_FRAMES frames so a
# single frame of white UI can never start the tracker.
TARGET_TAKEOVER_SECONDS = 4.0
AWAIT_TARGET_SECONDS = 4.0
_MIN_TARGET_SIZE = 16
# The popup also shows the white countdown digits (measured ~52x47 at the
# 1366px HUD reference) beside the real fading target (~89x99 there), so the
# engine demands a clearly bigger blob than the digits before seeding Cutie.
_MIN_LIE_TARGET_SIZE = 64
_CANDIDATE_STABLE_FRAMES = 2

# Geometry of the lie popup ("second window"), measured from the operator's
# 1366x768 client captures of a real lie event: a centred 767x598 popup with
# bright chrome, a gold/brown body and the fading white target inside.  The
# live frame is searched for that popup on every await-step; these constants
# provide the centred fallback and the reference every popup-derived number is
# recalculated from (64px countdown guard off the 767px reference width).
#
# The popup is a game-UI preset, not a fixed pixel box: 1920x1080 shares the
# 1366x768 preset but the game renders that preset SHRUNK (its 1075-wide UI
# reference), so the whole popup geometry/hud numbers have to be recalculated
# for it from the 1366 preset.  Preset factors live here - verify/adjust them
# against a real capture when a new preset shows up.
_HUD_REFERENCE_WIDTH = 1366
LIE_WINDOW_REFERENCE_CLIENT = (1366, 768)
LIE_WINDOW_REFERENCE_SIZE = (767, 598)
LIE_PRESET_UI_WIDTHS = {1920: 1075.0}
LIE_PRESET_MATCH_TOLERANCE = 8


def lie_ui_scale(client_width: int) -> float:
    """Popup/HUD scale for a client width (the 1366x768 preset is 1.0).

    Presets listed in :data:`LIE_PRESET_UI_WIDTHS` render the same UI preset
    at their own (smaller) UI width; everything else follows the HUD curve
    (shrinking below the 1366px reference, fixed above it).
    """

    try:
        width = int(client_width)
    except (TypeError, ValueError):
        return 1.0
    for preset_width, preset_ui_width in LIE_PRESET_UI_WIDTHS.items():
        if abs(width - preset_width) <= LIE_PRESET_MATCH_TOLERANCE:
            return max(0.05, preset_ui_width / float(_HUD_REFERENCE_WIDTH))
    return hud_scale(width)

# Optional self-start bell: while armed and idle, a compact white popup that
# appears and stays visible across two watcher samples can start the same
# attention sequence, so the #c9ced0 slice rule is not strictly required.
# False bells are cheap: the engine still requires the box to persist and
# the attention window just expires quietly when nothing confirms.
_NOVELTY_BELL_ENABLED = True
_NOVELTY_SCAN_INTERVAL = 0.4      # seconds between watcher frame samples
_NOVELTY_MIN_SIGHTINGS = 2        # stable sightings before a bell fires
_NOVELTY_FORGET_SECONDS = 3.0     # gap that resets the sighting counter
_NOVELTY_COOLDOWN_SECONDS = 10.0  # quiet time after a sequence ends

# Cutie itself is resolution-independent and its inference core already caps
# the internal size at 480.  Warming with this small, fixed frame means the
# optional feature starts preparing as soon as the assistant launches: it no
# longer waits for a game capture (which may not exist until patrol starts).
_STARTUP_WARMUP_SIZE = (480, 270)


_MODEL_CACHE: dict = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _package_tracker_device() -> Optional[str]:
    """Return the tracker device locked by a release package, if any."""

    try:
        declared = str(
            json.loads(_RELEASE_MANIFEST.read_text(encoding="utf-8-sig"))
            .get("tracker_runtime", "")
        ).strip().lower()
        if declared in {"cpu", "cuda"}:
            return declared
    except (OSError, ValueError, TypeError):
        pass
    return None


def _get_models(
    width: int, height: int, *, device: Optional[str] = None
):
    """Load Cutie + dense flow once per process and reuse them.

    Cutie's ``get_default_model`` runs Hydra ``initialize`` which fails on a
    second call in the same process, so a lie event hours later must not
    reload from scratch.  Engines are strictly sequential (never
    concurrent), so sharing one model instance is safe.
    """

    # A published CPU/CUDA package owns one explicit model cache and always
    # warms that declared device.  Development checkouts retain the automatic
    # unsuffixed cache so their two visual comparison testers stay isolated.
    if device is None:
        device = _package_tracker_device()
    suffix = "" if device is None else f":{str(device).strip().lower()}"
    cutie_key = "cutie" + suffix
    flow_key = "flow" + suffix
    with _MODEL_CACHE_LOCK:
        if cutie_key not in _MODEL_CACHE:
            try:
                from hydra.core.global_hydra import GlobalHydra

                GlobalHydra.instance().clear()
            except Exception:
                pass  # first load usually needs no clear
            from realtime_fade_tracker.core import load_and_warm_models

            _MODEL_CACHE[cutie_key], _MODEL_CACHE[flow_key] = load_and_warm_models(
                width, height, device=device
            )
        return _MODEL_CACHE[cutie_key], _MODEL_CACHE[flow_key]


def load_models_for_comparison(device: str) -> tuple[object, object]:
    """Build an isolated CPU/CUDA model pair for the visual comparison UI.

    The normal live tracker remains in the default cache.  Keeping a separate
    explicit-device cache lets the operator compare the exact same video with
    CPU and CUDA without changing the device used by automatic lie passing.
    """

    requested = str(device).strip().lower()
    supported, reason = probe_tracker_device(requested)
    if not supported:
        raise RuntimeError(reason)
    for path in (str(_TARGET_TRACKER_DIR), str(_TARGET_TRACKER_DIR / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    return _get_models(*_STARTUP_WARMUP_SIZE, device=requested)


# Cutie's model config builds a torchvision ResNet50 pixel encoder and a
# ResNet18 mask encoder.  Upstream would download these through torch.hub on
# first use; releases must resolve them locally because the assistant runs
# under pythonw (console streams are None, so the download progress write
# crashes) and may be offline.  The offline bundle now ships both files.
_RESNET_BACKBONE_FILES = ("resnet50-19c8e357.pth", "resnet18-5c106cde.pth")


def _candidate_weight_dirs():
    """Directories that may hold tracker weights, most persistent first."""

    dirs = []
    try:
        import sysconfig

        dirs.append(Path(sysconfig.get_paths()["purelib"]) / "weights")
    except Exception:
        pass
    dirs.append(_TARGET_TRACKER_DIR / "offline_bundle" / "weights")
    try:
        import torch.hub

        dirs.append(Path(torch.hub.get_dir()) / "checkpoints")
    except Exception:
        pass
    return dirs


def probe_auto_lie_environment() -> Tuple[bool, str]:
    """Return ``(supported, reason)`` for the auto lie-pass feature.

    Called once when the 附加功能 panel is built; must stay cheap enough
    for UI startup.  Never raises.  Reason strings include the running
    interpreter so an environment mismatch (wrong folder / wrong venv)
    is immediately visible in the UI.
    """

    interpreter = sys.executable or "?"
    try:
        import torch  # noqa: F401
    except Exception as exc:
        return False, "缺少 PyTorch: %s (python=%s)" % (exc, interpreter)
    try:
        import cv2  # noqa: F401
        import cutie  # noqa: F401
    except Exception as exc:
        return False, "缺少 cutie/opencv: %s (python=%s)" % (exc, interpreter)
    if not (_TARGET_TRACKER_DIR / "src" / "realtime_fade_tracker").is_dir():
        return False, "缺少 target_tracker 组件目录"

    def _find_weight(name: str) -> Optional[Path]:
        for dir_path in _candidate_weight_dirs():
            candidate = dir_path / name
            if candidate.is_file():
                return candidate
        return None

    if _find_weight("cutie-base-mega.pth") is None:
        return False, (
            "缺少模型权重 cutie-base-mega.pth "
            "(site-packages/weights 或随包 target_tracker/offline_bundle/weights)"
        )
    missing_backbones = [
        name for name in _RESNET_BACKBONE_FILES if _find_weight(name) is None
    ]
    if missing_backbones:
        return False, (
            "缺少 Cutie 骨干网络权重 %s（pythonw 下无法联网下载）；"
            "请重新运行 安装.bat" % "、".join(missing_backbones)
        )
    try:
        mode = "CUDA" if torch.cuda.is_available() else "CPU"
        version = torch.__version__
    except Exception:
        mode, version = "CPU", "?"
    return True, "运行设备: %s (torch %s, python=%s)" % (
        mode, version, interpreter
    )


def probe_tracker_device(device: str) -> Tuple[bool, str]:
    """Validate a tracker device without depending on the UI or patrol state."""

    requested = str(device).strip().lower()
    if requested == "cpu":
        return True, "CPU"
    if requested != "cuda":
        return False, "未知追踪设备: %s" % device
    try:
        import torch
    except Exception as exc:
        return False, "无法导入 PyTorch: %s" % exc
    if not torch.cuda.is_available():
        return False, "当前 Python 环境中的 PyTorch 未检测到可用 CUDA"
    return True, "CUDA"


def make_seed_mask(
    frame_shape: Tuple[int, int, int],
    box: Tuple[int, int, int, int],
) -> Any:
    """Build a binary seed mask (uint8) from a detected lie-square box.

    ``frame_shape`` is (height, width, channels); ``box`` is
    (left, top, width, height) in frame pixels, clipped to the frame here.
    """

    import numpy as np

    height, width = frame_shape[:2]
    left, top, box_width, box_height = box
    left = max(0, min(int(left), width - 1))
    top = max(0, min(int(top), height - 1))
    right = min(width, max(left + 1, int(left + box_width)))
    bottom = min(height, max(top + 1, int(top + box_height)))
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[top:bottom, left:right] = 1
    return mask


def _bgr_from_pil(image: Any) -> Any:
    """Convert a PIL RGB image into an OpenCV BGR uint8 array."""

    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _boxes_similar(a, b, tolerance: int = 6) -> bool:
    """True when two component boxes sit at the same spot with similar size."""

    if a is None or b is None:
        return False
    return (
        abs(a[0] - b[0]) <= tolerance
        and abs(a[1] - b[1]) <= tolerance
        and abs(a[2] - b[2]) <= tolerance
        and abs(a[3] - b[3]) <= tolerance
    )


def hud_scale(width: int) -> float:
    """HUD scale factor for a client ``width`` (1.0 at/above the reference)."""

    return min(1.0, max(0.0, float(width) / float(_HUD_REFERENCE_WIDTH)))


def _min_lie_target_size(client_width: int) -> int:
    """Smallest accepted white-target side for this client (countdown guard).

    The popup's white countdown digits are much smaller than the real fading
    target, so the engine only seeds on a blob that is clearly bigger.  The
    1366-preset value is recalculated with :func:`lie_ui_scale`, so a shrunk
    preset such as 1920x1080 keeps the same separation.
    """

    scaled = int(round(_MIN_LIE_TARGET_SIZE * lie_ui_scale(client_width)))
    return max(_MIN_TARGET_SIZE, scaled)


def lie_window_box(width: int, height: int) -> Tuple[int, int, int, int]:
    """Centred fallback box of the lie popup in client pixels.

    Recalculated from the measured 1366x768 preset (767x598) through
    :func:`lie_ui_scale`, so other presets - including the shrunk 1920x1080
    one - get their own popup size instead of the reference box.
    """

    width = max(1, int(width))
    height = max(1, int(height))
    scale = lie_ui_scale(width)
    box_width = max(1, min(width, int(round(LIE_WINDOW_REFERENCE_SIZE[0] * scale))))
    box_height = max(1, min(height, int(round(LIE_WINDOW_REFERENCE_SIZE[1] * scale))))
    return (
        max(0, (width - box_width) // 2),
        max(0, (height - box_height) // 2),
        box_width,
        box_height,
    )


def _find_lie_window_box(bgr: Any) -> Optional[Tuple[int, int, int, int]]:
    """Locate the lie popup in a live client frame -> ``(x, y, w, h)`` or None.

    The popup is a large, centred panel drawn with bright chrome.  Picking
    the biggest near-white structure that is centred and large enough works
    at any client resolution, so the tracker crop follows the real popup.
    Returns None when nothing convincing is on screen; the caller then falls
    back to the centred :func:`lie_window_box`.
    """

    import cv2
    import numpy as np

    height, width = bgr.shape[:2]
    if width < 64 or height < 64:
        return None
    mask = np.asarray(bgr.min(axis=2) >= 210, dtype=np.uint8)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)
    best: Optional[Tuple[int, int, int, int]] = None
    best_score = 0.0
    for label in range(1, count):
        x, y, box_w, box_h, _area = (int(value) for value in stats[label])
        # Wide bounds on purpose: a preset like 1920x1080 renders the popup
        # shrunk, and the detection must still lock onto it.
        if box_w < width * 0.24 or box_w > width * 0.92:
            continue
        if box_h < height * 0.34 or box_h > height * 0.95:
            continue
        center_x, center_y = x + box_w / 2.0, y + box_h / 2.0
        if abs(center_x - width / 2.0) > width * 0.12:
            continue
        if abs(center_y - height / 2.0) > height * 0.12:
            continue
        score = float(box_w * box_h)
        if score > best_score:
            best_score, best = score, (x, y, box_w, box_h)
    return best


def _find_target_box(bgr: Any, min_side: int = _MIN_TARGET_SIZE):
    """Locate the real white lie-test box in a live client frame.

    The #c9ced0 pre-window slice is grey (V~208), so it can never match the
    bright-white rule here (S<=60, V>=235 - the tracker's own white test).
    This reuses the tracker's "compact popup" idea: square-ish aspect, solid
    fill, bounded size/span, centre bias.  Returns (left, top, width, height)
    of the best candidate or None.
    """

    import math

    import cv2
    import numpy as np

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    white = np.asarray(
        (hsv[:, :, 1] <= 60) & (hsv[:, :, 2] >= 235), dtype=np.uint8
    )
    white = cv2.morphologyEx(
        white,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    height, width = white.shape[:2]
    frame_area = width * height
    max_span = min(width, height) * 0.35
    center_x, center_y = width / 2.0, height / 2.0
    count, labels, stats, centers = cv2.connectedComponentsWithStats(
        white, connectivity=8
    )
    best_box = None
    best_score = 0.0
    for label in range(1, count):
        x, y, box_w, box_h, area = (int(value) for value in stats[label])
        if area < min_side * min_side or box_w < min_side or box_h < min_side:
            continue
        if area > frame_area * 0.05 or max(box_w, box_h) > max_span:
            continue
        aspect = box_w / max(1.0, box_h)
        if aspect < 0.4 or aspect > 2.5:
            continue
        fill = area / max(1.0, box_w * box_h)
        if fill < 0.35:
            continue
        distance = float(
            math.hypot(centers[label][0] - center_x, centers[label][1] - center_y)
            / max(width, height)
        )
        score = area * (0.5 + 0.5 * fill) * max(0.0, 1.0 - distance * 1.5)
        if score > best_score:
            best_score = score
            best_box = (x, y, box_w, box_h)
    return best_box


def _novelty_next_state(box, previous, now: float):
    """Advance the novelty-watcher sighting state for one sample.

    ``previous`` is ``(box, count, first_seen_at)`` or None.  The counter
    restarts when the box moves (different position), disappears long enough
    to age past the forget window, or no box is seen at all.  Returns the new
    state tuple, or None when nothing is currently being watched.
    """

    if (
        box is not None
        and previous is not None
        and _boxes_similar(box, previous[0])
        and now - previous[2] <= _NOVELTY_FORGET_SECONDS
    ):
        return (box, previous[1] + 1, previous[2])
    if box is None:
        return None
    return (box, 1, now)


class AutoLieWorker(threading.Thread):
    """Drives the fade tracker once per lie event and aims the real mouse.

    The heavy tracking models are warmed in this worker as soon as the
    assistant starts, before the UI is opened.  A lie square is short-lived,
    so the event path must only seed the already-warm tracker, follow the
    fading target with the mouse, click once, and stop when the square is
    gone / the target is lost / the sequence times out.  The cursor is never
    moved back.
    """

    def __init__(
        self,
        frames: "queue.Queue[Any]",
        stop_event: threading.Event,
        *,
        enabled: bool = False,
    ) -> None:
        super().__init__(name="auto-lie-worker", daemon=True)
        self.frames = frames
        self.stop_event = stop_event
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._active = False
        self._pending: Optional[Tuple[Tuple[int, int, int, int], Any]] = None
        self._request_event = threading.Event()
        self._square_cleared_at: Optional[float] = None
        self._models_ready = threading.Event()
        self._warmup_failure: Optional[str] = None
        self._warmup_started_at: Optional[float] = None
        # Novelty-bell watcher state (self-start when no slice rule fires).
        self._novelty_last = None
        self._last_novelty_scan = 0.0
        self._last_watched_sequence = -1
        self._last_sequence_end: Optional[float] = None

    # ------------------------------------------------------------- public API

    def set_enabled(self, enabled: bool) -> None:
        """Apply the 自动过测谎 checkbox live."""

        with self._lock:
            self._enabled = bool(enabled)
        if enabled:
            self._request_event.set()
        LOG.info(
            "auto-lie: module %s; waiting for 测谎 detection events "
            "(报警-测谎 需开启，勾选本项后会自动开启)",
            "armed" if enabled else "disarmed",
        )

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def warmup_failure(self) -> Optional[str]:
        """Startup warmup error, if the optional tracker could not prepare."""

        return self._warmup_failure

    @property
    def warmup_elapsed_seconds(self) -> float:
        """Seconds spent preparing Cutie, or zero before worker startup."""

        started = self._warmup_started_at
        return 0.0 if started is None else max(0.0, time.monotonic() - started)

    def preloaded_models(self) -> Optional[tuple[object, object]]:
        """Return the startup-warmed models for the in-process test player.

        The demo must share this cache rather than starting a second Python
        process, otherwise it pays the same cold Cutie load as a real event.
        """

        if not self._models_ready.is_set():
            return None
        device = _package_tracker_device()
        suffix = "" if device is None else f":{device}"
        with _MODEL_CACHE_LOCK:
            cutie = _MODEL_CACHE.get("cutie" + suffix)
            flow = _MODEL_CACHE.get("flow" + suffix)
        if cutie is None or flow is None:
            return None
        return cutie, flow

    def on_lie_seen(
        self,
        match: Optional[Tuple[int, int, int, int]],
        frame: Any,
    ) -> None:
        """LieDetectorWorker callback: a square appeared (match) or cleared.

        Called from the lie detector thread.  Only lightweight state
        changes happen here; all GPU work runs in this worker's thread.
        """

        with self._lock:
            enabled = self._enabled
            active = self._active
            pending = self._pending
            if match is not None:
                if enabled and not active and pending is None:
                    self._pending = (match, frame)
                    self._request_event.set()
            elif active:
                # The slice is only the event alarm; its disappearance must
                # never stop the tracking of the fading target.
                self._square_cleared_at = time.monotonic()
                LOG.info(
                    "auto-lie: lie alarm slice cleared (bell only; tracking "
                    "continues)"
                )
        if match is not None:
            if enabled and not active and pending is None:
                LOG.info(
                    "auto-lie: pre-window slice event queued bbox=%s -> "
                    "scanning for the white target",
                    match,
                )
            else:
                LOG.info(
                    "auto-lie: lie square event IGNORED "
                    "(enabled=%s active=%s pending=%s)",
                    enabled, active, pending is not None,
                )

    # ------------------------------------------------------------- run loop

    @staticmethod
    def _ensure_tracker_paths() -> None:
        """Make the bundled tracker importable before the startup warmup."""

        for path in (str(_TARGET_TRACKER_DIR), str(_TARGET_TRACKER_DIR / "src")):
            if path not in sys.path:
                sys.path.insert(0, path)

    def _warm_models_at_startup(self) -> None:
        """Load and warm models immediately, without waiting for game capture.

        This deliberately runs even while 自动过测谎 is unchecked.  The
        checkbox controls mouse automation, not model residency: loading
        Cutie after a lie square appears can take long enough for the target
        to have faded before the tracker receives its first frame.
        """

        width, height = _STARTUP_WARMUP_SIZE
        self._warmup_started_at = time.monotonic()
        LOG.info(
            "auto-lie: startup %s model warmup beginning immediately (%dx%d)",
            (_package_tracker_device() or "auto").upper(),
            width,
            height,
        )
        try:
            self._ensure_tracker_paths()
            _get_models(width, height)
            self._models_ready.set()
            LOG.info(
                "auto-lie: startup model warmup complete in %.1fs (%dx%d); "
                "ready before lie detection",
                time.monotonic() - self._warmup_started_at,
                width,
                height,
            )
        except Exception as exc:
            self._warmup_failure = str(exc)
            LOG.exception("auto-lie: startup model warmup failed")

    def run(self) -> None:
        LOG.info(
            "auto-lie: worker started (module %s; enable 自动过测谎 in "
            "附加功能 to arm)",
            "armed" if self.enabled else "disarmed",
        )
        # This is intentionally before the normal event loop.  It runs on
        # the worker thread, never Tk's thread, so UI startup stays responsive
        # while the model is prepared for the first real lie event.
        self._warm_models_at_startup()
        while not self.stop_event.is_set():
            with self._lock:
                enabled = self._enabled
                pending = self._pending
            if enabled and pending is not None:
                with self._lock:
                    self._pending = None
                    self._active = True
                engine: Optional[_LieSequenceEngine] = None
                try:
                    engine = _LieSequenceEngine(self, pending[0], pending[1])
                    engine.run_until_end()
                except Exception:
                    LOG.exception("auto-lie: sequence failed")
                finally:
                    if engine is not None:
                        engine.dispose()
                    self._last_sequence_end = time.monotonic()
                    with self._lock:
                        self._active = False
                        self._square_cleared_at = None
                continue
            # Optional self-start bell while idle: no slice event needed.
            self._maybe_novelty_bell()
            self._request_event.wait(timeout=0.25)
            self._request_event.clear()
        LOG.info("auto-lie: worker stopped")

    def _maybe_novelty_bell(self) -> None:
        """Cheap self-start watcher: a new stable white popup rings a bell.

        Runs only while the module is armed and idle.  Consumes at most one
        frame per scan tick; the real confirmation still happens in the
        engine's phase 1, so a false bell is harmless (the attention window
        expires quietly).
        """

        if not _NOVELTY_BELL_ENABLED:
            return
        now = time.monotonic()
        if now - self._last_novelty_scan < _NOVELTY_SCAN_INTERVAL:
            return
        self._last_novelty_scan = now
        with self._lock:
            enabled = self._enabled
            active = self._active
            pending = self._pending
        if not enabled or active or pending is not None:
            return
        if (
            self._last_sequence_end is not None
            and now - self._last_sequence_end < _NOVELTY_COOLDOWN_SECONDS
        ):
            return
        try:
            frame = self.frames.get_nowait()
        except queue.Empty:
            return
        sequence = int(getattr(frame, "sequence", -1))
        if sequence <= self._last_watched_sequence:
            return
        self._last_watched_sequence = sequence
        try:
            bgr = _bgr_from_pil(frame.image)
            box = _find_target_box(
                bgr, min_side=_min_lie_target_size(int(bgr.shape[1]))
            )
        except Exception:
            return
        self._novelty_last = _novelty_next_state(box, self._novelty_last, now)
        state = self._novelty_last
        if state is None or state[1] < _NOVELTY_MIN_SIGHTINGS:
            return
        self._novelty_last = None
        with self._lock:
            if not self._enabled or self._active or self._pending is not None:
                return
            self._pending = (state[0], frame)
            self._request_event.set()
        LOG.info(
            "auto-lie: novelty bell - white popup %s appeared; starting "
            "sequence",
            state[0],
        )


class _LieSequenceEngine:
    """One lie-pass sequence: alarm delay, seed Cutie, follow the target.

    Mouse movement only - no click is ever sent.  Created lazily so
    importing this module never touches torch/cutie.
    """

    def __init__(
        self,
        owner: AutoLieWorker,
        match: Tuple[int, int, int, int],
        seed_frame: Any,
    ) -> None:
        self.owner = owner
        self._started_at = time.monotonic()
        self._ended = False
        self._end_reason: Optional[str] = None
        self._tracker: Any = None
        self._aim: Any = None
        self._suspended_aims: list = []
        self._last_sequence = -1
        self._last_live_at = time.monotonic()
        self._low_conf_since: Optional[float] = None
        self._smooth: Optional[Tuple[float, float]] = None
        # Phase 1 "await-target": the #c9ced0 slice is only the pre-window
        # bell.  Live frames are scanned for the actual bright-white box and
        # Cutie is seeded only once it is confirmed, so the tracker never
        # starts on the wrong object (phase 2 "tracking").
        self._phase = "await-target"
        self._candidate_box: Optional[Tuple[int, int, int, int]] = None
        self._candidate_hits = 0
        self._takeover_logged = False

        self._ensure_paths()
        seed_bgr = _bgr_from_pil(seed_frame.image)
        self._height, self._width = seed_bgr.shape[:2]
        left, top, box_width, box_height = match
        if box_width < _MIN_SEED_SIZE or box_height < _MIN_SEED_SIZE:
            raise ValueError("lie slice too small to start a sequence")
        # The tracker only ever sees the lie popup (the "second window"),
        # never the whole game client.
        self._window_box: Tuple[int, int, int, int] = (
            _find_lie_window_box(seed_bgr)
            or lie_window_box(self._width, self._height)
        )
        self._min_target_side = _min_lie_target_size(self._width)
        self._crop_size: Tuple[int, int] = (self._width, self._height)
        # Frames older than the pre-window event are irrelevant; newer ones
        # may already contain the white target box and must be kept.
        self._last_sequence = int(getattr(seed_frame, "sequence", -1))
        self._drain_stale_frames()
        LOG.info(
            "auto lie pass: alarm slice %s seen; lie window %s (%dx%d); "
            "Cutie takes over the target %.1fs after the alarm (grace %.1fs)",
            (left, top, box_width, box_height),
            self._window_box,
            self._crop_size[0],
            self._crop_size[1],
            TARGET_TAKEOVER_SECONDS,
            AWAIT_TARGET_SECONDS,
        )

    def _window_crop(self, bgr: Any) -> Any:
        """Return the lie-popup crop of a client frame (tracker input space)."""

        left, top, width, height = self._window_box
        frame_height, frame_width = bgr.shape[:2]
        left = max(0, min(int(left), frame_width - 1))
        top = max(0, min(int(top), frame_height - 1))
        right = max(left + 1, min(frame_width, left + int(width)))
        bottom = max(top + 1, min(frame_height, top + int(height)))
        return bgr[top:bottom, left:right]

    @staticmethod
    def _ensure_paths() -> None:
        for path in (
            str(_TARGET_TRACKER_DIR),
            str(_TARGET_TRACKER_DIR / "src"),
        ):
            if path not in sys.path:
                sys.path.insert(0, path)

    def _drain_stale_frames(self) -> None:
        """Drop queued frames older than the pre-window event frame."""

        while True:
            try:
                frame = self.owner.frames.get_nowait()
            except queue.Empty:
                return
            if int(getattr(frame, "sequence", -1)) > self._last_sequence:
                self.owner.frames.put_nowait(frame)
                return

    # ------------------------------------------------------------- stepping

    def _is_stop_requested(self) -> bool:
        """Only a stop request or unchecking 自动过测谎 stops a sequence.

        The #c9ced0 alarm slice clearing is deliberately NOT a stop signal:
        it disappears as soon as the real popup opens, while the target - and
        its fade - are still to come.
        """

        if self.owner.stop_event.is_set():
            return True
        with self.owner._lock:
            return not self.owner._enabled

    def run_until_end(self) -> None:
        """Drive the sequence on the worker thread until it ends."""

        while not self._ended and not self.owner.stop_event.is_set():
            self.step_once()

    def step_once(self) -> None:
        """Consume one frame: await the white box, then track and click."""

        if self._ended:
            return
        if self._is_stop_requested():
            self.end("disabled/stopped or lie window cleared")
            return
        if time.monotonic() - self._started_at > MAX_SEQUENCE_SECONDS:
            self.end("timeout")
            return
        try:
            frame = self.owner.frames.get(timeout=0.15)
        except queue.Empty:
            if time.monotonic() - self._last_live_at > STALE_END_SECONDS:
                self.end("capture stalled")
            return
        sequence = int(getattr(frame, "sequence", -1))
        if sequence <= self._last_sequence:
            return
        self._last_sequence = sequence
        bgr = _bgr_from_pil(frame.image)
        if bgr.shape[:2] != (self._height, self._width):
            self.end("client size changed")
            return
        if self._phase == "await-target":
            self._await_target_step(frame, bgr)
            return
        self._track_step(sequence, frame, bgr)

    def _await_target_step(self, frame: Any, bgr: Any) -> None:
        """Wait out the alarm, then find the white target and seed Cutie."""

        now = time.monotonic()
        elapsed = now - self._started_at
        self._last_live_at = now  # frames are flowing; never "stalled" here
        if elapsed > TARGET_TAKEOVER_SECONDS + AWAIT_TARGET_SECONDS:
            self.end("no white target box appeared after the takeover delay")
            return
        # The popup itself is re-detected on every fresh frame so the crop
        # always hugs the real "second window" (the tracker never sees the
        # rest of the game client).
        detected = _find_lie_window_box(bgr)
        if detected is not None:
            self._window_box = detected
        crop = self._window_crop(bgr)
        self._crop_size = (int(crop.shape[1]), int(crop.shape[0]))
        if elapsed < TARGET_TAKEOVER_SECONDS:
            # The alarm is still running: nothing is seeded yet, so the
            # countdown digits or any early white UI cannot start Cutie.
            return
        if not self._takeover_logged:
            self._takeover_logged = True
            LOG.info(
                "auto lie pass: takeover delay %.1fs elapsed -> looking for "
                "the white target in %s",
                TARGET_TAKEOVER_SECONDS,
                self._window_box,
            )
        box = _find_target_box(crop, min_side=self._min_target_side)
        if box is None or not _boxes_similar(box, self._candidate_box):
            self._candidate_box = box
            self._candidate_hits = 0 if box is None else 1
            return
        self._candidate_hits += 1
        if self._candidate_hits < _CANDIDATE_STABLE_FRAMES:
            return
        self._start_tracking(frame, bgr, box)

    def _start_tracking(self, frame: Any, bgr: Any, box) -> None:
        """Phase 2: seed Cutie with the confirmed white box and arm the mouse."""

        from realtime_fade_tracker.core import (
            LatestFrameTrackingWorker,
        )

        crop = self._window_crop(bgr)
        mask = make_seed_mask(crop.shape, box)
        if int(mask.sum()) <= 0:
            self.end("white target mask is empty")
            return
        self._crop_size = (int(crop.shape[1]), int(crop.shape[0]))
        loaded_at = time.monotonic()
        cutie, dense_flow = _get_models(self._crop_size[0], self._crop_size[1])
        self._tracker = LatestFrameTrackingWorker(
            cutie, dense_flow, crop.copy(), mask,
        )
        self._initial = self._tracker.latest()

        from mouse_aim_controller import (
            MouseAimController,
            suspend_others_except,
        )

        self._aim = MouseAimController(self._crop_size[0], self._crop_size[1])
        self._apply_region(frame)
        # While this sequence drives the cursor, pause every other live
        # controller (e.g. an open 测试测谎 demo window) and restore later.
        self._suspended_aims = suspend_others_except(self._aim)
        self._phase = "tracking"
        LOG.info(
            "auto lie pass: lie window %s -> crop %dx%d; white target box %s "
            "confirmed after %.2fs -> Cutie seeded (models %.1fs; initial "
            "state=%s conf=%.3f)",
            self._window_box,
            self._crop_size[0],
            self._crop_size[1],
            box,
            time.monotonic() - self._started_at,
            time.monotonic() - loaded_at,
            getattr(self._initial, "state", "?"),
            float(getattr(self._initial, "confidence", 0.0)),
        )

    def _apply_region(self, frame: Any) -> None:
        """Map the lie-popup crop onto the screen for the mouse aim thread.

        The tracker works in crop pixel coordinates, so the aim region is the
        popup's own screen rectangle - never the whole client rectangle.
        """

        if self._aim is None:
            return
        rect = getattr(frame, "window_rect", None)
        if rect is None or len(rect) != 4:
            return
        left, top, width, height = self._window_box
        scale_x = (float(rect[2]) - float(rect[0])) / max(1.0, float(self._width))
        scale_y = (float(rect[3]) - float(rect[1])) / max(1.0, float(self._height))
        self._aim.set_region(
            int(round(float(rect[0]) + left * scale_x)),
            int(round(float(rect[1]) + top * scale_y)),
            int(round(float(rect[0]) + (left + width) * scale_x)),
            int(round(float(rect[1]) + (top + height) * scale_y)),
        )

    def _track_step(self, sequence: int, frame: Any, bgr: Any) -> None:
        """Phase 2 body: submit to Cutie, aim the mouse, click once."""

        import numpy as np
        from realtime_fade_tracker.realtime import TrackingResult

        if self._tracker is None:
            self.end("tracker missing")
            return
        crop = self._window_crop(bgr)
        self._tracker.submit(sequence, crop)
        result = TrackingResult.from_core(self._tracker.latest())

        # Note: the target DIMS as the event runs.  A dimmed (or no longer
        # bright-white) target is exactly what Cutie/dense-residual tracking
        # must keep following, so its whiteness is never an end condition.
        crop_width, crop_height = self._crop_size
        lag = max(0, sequence - result.frame_index)
        prediction = min(lag, 2)
        x = float(np.clip(
            result.x + result.velocity_x * prediction, 0, crop_width - 1
        ))
        y = float(np.clip(
            result.y + result.velocity_y * prediction, 0, crop_height - 1
        ))
        self._apply_region(frame)
        if self._smooth is None:
            self._smooth = (x, y)
        else:
            self._smooth = (
                self._smooth[0] + SMOOTH_ALPHA * (x - self._smooth[0]),
                self._smooth[1] + SMOOTH_ALPHA * (y - self._smooth[1]),
            )
        self._aim.push_target(
            self._smooth[0],
            self._smooth[1],
            float(result.confidence),
            str(result.state),
        )
        now = time.monotonic()
        if float(result.confidence) >= END_CONFIDENCE:
            self._last_live_at = now
            self._low_conf_since = None
        else:
            if self._low_conf_since is None:
                self._low_conf_since = now
            elif now - self._low_conf_since > LOW_CONF_END_SECONDS:
                self.end("target lost")
                return
        if now - self._last_live_at > STALE_END_SECONDS:
            self.end("no fresh tracking result")

    # ------------------------------------------------------------- cleanup

    def end(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._end_reason = reason
        LOG.info(
            "auto lie pass: sequence ended (%s) after %.1fs; cursor left "
            "in place",
            reason,
            time.monotonic() - self._started_at,
        )
        self.dispose()

    def dispose(self) -> None:
        """Release GPU tracker and aim threads exactly once."""

        if self._aim is not None:
            try:
                self._aim.close()
            except Exception:
                LOG.warning("auto lie pass aim close failed", exc_info=True)
            self._aim = None
        if self._tracker is not None:
            try:
                self._tracker.close()
            except Exception:
                LOG.warning("auto lie pass tracker close failed", exc_info=True)
            self._tracker = None
        if self._suspended_aims:
            for controller in self._suspended_aims:
                try:
                    controller.set_enabled(True, silent=True)
                except Exception:
                    LOG.warning(
                        "auto lie pass aim restore failed", exc_info=True
                    )
            self._suspended_aims = []


__all__ = [
    "AutoLieWorker",
    "lie_window_box",
    "probe_auto_lie_environment",
    "make_seed_mask",
]
