"""自动重连 (automatic reconnect): 掉线 -> login page -> world -> channel -> log in.

The assistant already knows when the game dropped the character: the 掉线 event of the
character detector (its yellow marker disappears for several frames).  That event alone is
not proof, so this worker requires a SECOND sign before it touches anything: the game window
must show the login page's own BASE COLOUR (screenshots/login_page_target.jpg is a crop of
the page's cream background, so it defines a colour to look for - not a shape to correlate).
Only then does it act:

  1. press Enter, wait 3s (the login page closes),
  2. first select window: CLICK the row of the chosen world (the row is computed from the
     measured list geometry - index * row pitch - not by reading text),
  3. that opens the second select window: CLICK the first channel (the circle at the top
     of the list),
  4. the keyboard does the rest: move to the target channel with right/down, Enter, wait 2s,
     Enter.

Every step is reported to the UI through a result queue, so the operator can see what the
worker did and where it stopped.  Nothing here uses Cutie or any tracking model.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from image_io import save_screenshot

LOG = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SCREENSHOTS_DIR = ROOT / "screenshots"
# A crop of the login page's cream background: it is the COLOUR reference, not a shape.
LOGIN_TEMPLATE_NAME = "login_page_target.jpg"
# Where the reference is looked for, in this order.  The release package does not ship the
# personal screenshots folder, so the same file also travels inside recording-assets (the
# folder that IS shipped) - otherwise an installed copy would have no colour reference at all.
LOGIN_REFERENCE_PATHS = (
    SCREENSHOTS_DIR / LOGIN_TEMPLATE_NAME,
    ROOT / "recording-assets" / LOGIN_TEMPLATE_NAME,
)

# The worlds the operator can choose between (the order is the order of the list in the
# game's first select window).
WORLD_NAMES = ("蓝蜗牛", "蘑菇仔", "绿水灵", "漂漂猪", "小白兔")
CHANNEL_MIN = 1
CHANNEL_MAX = 60
CHANNEL_DEFAULT = 1
WORLD_DEFAULT = WORLD_NAMES[0]

# How the login page is recognised: the reference patch fixes the BASE COLOUR, and its own
# pixels fix the narrow RANGE around it (robust percentiles, widened by a small margin, so
# the range keeps the reference's texture instead of being a single flat value).
LOGIN_COLOUR_PERCENTILE_LOW = 1.0
LOGIN_COLOUR_PERCENTILE_HIGH = 99.0
LOGIN_COLOUR_MARGIN = 6
# ... then the game window must be that colour over ONE large single region.  Measured with
# the operator's own frames: the two select windows are 11.7 % (one 787x673 region of the
# page), the in-game frames 0.0-0.1 %, so 6 % separates them with a wide margin.
LOGIN_PAGE_MIN_FRACTION = 0.06
LOGIN_PAGE_MIN_PIXELS = 20_000
# The login page can take a moment to appear after the disconnect event.
LOGIN_CHECK_ATTEMPTS = 12
LOGIN_CHECK_INTERVAL_SECONDS = 1.0
# Wait after pressing Enter on the login page / after opening a select window.
LOGIN_WAIT_SECONDS = 3.0
SELECT_WINDOW_WAIT_SECONDS = 1.5
# Time a single key hold and the pause between two channel moves.
KEY_HOLD_SECONDS = 0.05
KEY_DELAY_SECONDS = 0.20
# The operator's sequence: ... -> channel moves -> Enter -> 2s -> Enter.
# The first Enter confirms the channel, the second one starts the login; the pause in
# between is what the game needs to show the character/loading screen.
CHANNEL_CONFIRM_WAIT_SECONDS = 2.0
# Pause after logging in, before the worker reports the reconnect as finished.
LOGIN_SETTLE_SECONDS = 3.0


@dataclass(frozen=True)
class ReconnectLayout:
    """Measured geometry of the reconnect screens, in 1366x768 client pixels.

    All positions are taken from the operator's own screenshots
    (screenshots/channel_select_first.jpg and channel_select_second.jpg) and are scaled to
    the live client by :meth:`scaled`, so the same numbers work on any client size.
    """

    reference_client: tuple[int, int] = (1366, 768)
    # The world list of the first select window: the centre of its first row, the row
    # pitch (how far the next world is below it) and the x of the row's centre.
    world_first_row_centre: tuple[int, int] = (0, 0)
    world_row_pitch: int = 0
    # The second select window: the centre of the first channel's circle and the grid
    # pitch of the channel cells.
    first_channel_centre: tuple[int, int] = (0, 0)
    channel_pitch: tuple[int, int] = (0, 0)
    channels_per_row: int = 1

    def scaled(self, client_size: tuple[int, int]) -> "ReconnectLayout":
        """The same layout for the live client size (measured 1366x768 preset)."""

        width, height = (int(value) for value in client_size)
        ref_width, ref_height = self.reference_client
        if width <= 0 or height <= 0:
            return self
        scale_x = width / float(ref_width)
        scale_y = height / float(ref_height)
        return ReconnectLayout(
            reference_client=(ref_width, ref_height),
            world_first_row_centre=(
                int(round(self.world_first_row_centre[0] * scale_x)),
                int(round(self.world_first_row_centre[1] * scale_y)),
            ),
            world_row_pitch=int(round(self.world_row_pitch * scale_y)),
            first_channel_centre=(
                int(round(self.first_channel_centre[0] * scale_x)),
                int(round(self.first_channel_centre[1] * scale_y)),
            ),
            channel_pitch=(
                int(round(self.channel_pitch[0] * scale_x)),
                int(round(self.channel_pitch[1] * scale_y)),
            ),
            channels_per_row=max(1, int(self.channels_per_row)),
        )

    def world_click_point(
        self, world_index: int, client_size: tuple[int, int]
    ) -> tuple[int, int]:
        """Where to click for world number ``world_index`` (0-based)."""

        layout = self.scaled(client_size)
        index = max(0, min(int(world_index), len(WORLD_NAMES) - 1))
        return (
            layout.world_first_row_centre[0],
            layout.world_first_row_centre[1] + index * layout.world_row_pitch,
        )

    def channel_click_point(
        self, channel: int, client_size: tuple[int, int]
    ) -> tuple[int, int]:
        """Where the channel ``channel`` (1-based) sits in the grid."""

        layout = self.scaled(client_size)
        number = max(CHANNEL_MIN, min(int(channel), CHANNEL_MAX)) - 1
        column = number % layout.channels_per_row
        row = number // layout.channels_per_row
        return (
            layout.first_channel_centre[0] + column * layout.channel_pitch[0],
            layout.first_channel_centre[1] + row * layout.channel_pitch[1],
        )

    def channel_key_moves(self, channel: int) -> list[str]:
        """The right/down keys that move the selection from channel 1 to ``channel``.

        The operator's rule: click the first channel, then let the keyboard do the rest
        moving right and down.
        """

        layout = self.scaled(self.reference_client)
        number = max(CHANNEL_MIN, min(int(channel), CHANNEL_MAX)) - 1
        per_row = max(1, layout.channels_per_row)
        return ["right"] * (number % per_row) + ["down"] * (number // per_row)

    @property
    def is_calibrated(self) -> bool:
        """True when the geometry was really measured.

        The built-in values are all zeros, and ``channel_key_moves`` with
        ``channels_per_row = 1`` would turn channel 15 into fourteen "down" presses - so an
        uncalibrated layout must stop the run instead of clicking the window corner (measured
        in the field: ``clicking world 蓝蜗牛 (row 1) at client (0, 0)``).
        """

        return (
            tuple(self.world_first_row_centre) != (0, 0)
            and int(self.world_row_pitch) > 0
            and tuple(self.first_channel_centre) != (0, 0)
            and int(self.channel_pitch[0]) > 0
            and int(self.channel_pitch[1]) > 0
            and int(self.channels_per_row) > 1
        )


def layout_path() -> Path:
    """Where the calibrated layout lives (written by work/reconnect_calibrate.py)."""

    return ROOT / "reconnect_layout.json"


# The five points that define the click geometry, in the order the panel asks for them.  The
# operator puts the real mouse where the game shows that entry and the app records it: this
# needs no screenshots and no command line, which matters because the game (and therefore the
# frames) only exists on the operator's own machine.
CALIBRATION_STEPS: tuple[tuple[str, str], ...] = (
    ("world_row_0", "世界列表第 1 行（蓝蜗牛）"),
    ("world_row_1", "世界列表第 2 行（蘑菇仔）"),
    ("channel_1", "频道列表第 1 个频道"),
    ("channel_2", "第 2 个频道（同一行右侧）"),
    ("channel_6", "第 6 个频道（下一行的第一个）"),
)
# MapleStory's channel list is five channels per row; step 5 is the first channel of row 2,
# which is what makes the right/down move count correct.
CHANNELS_PER_ROW = 5


def cursor_screen_position() -> tuple[int, int]:
    """Where the mouse cursor is, in screen pixels."""

    import ctypes

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = _Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return int(point.x), int(point.y)


def write_layout(layout: ReconnectLayout, path: Optional[Path] = None) -> Path:
    """Write a measured layout as JSON (the file load_layout reads)."""

    import json

    target = Path(path) if path is not None else layout_path()
    world_rows = [
        list(layout.world_click_point(index, layout.reference_client))
        for index in range(len(WORLD_NAMES))
    ]
    data = {
        "reference_client": [int(layout.reference_client[0]),
                             int(layout.reference_client[1])],
        "world_rows": world_rows,
        "first_channel_centre": [int(layout.first_channel_centre[0]),
                                 int(layout.first_channel_centre[1])],
        "channel_pitch": [int(layout.channel_pitch[0]), int(layout.channel_pitch[1])],
        "channels_per_row": int(layout.channels_per_row),
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("auto reconnect: layout written to %s (%d world rows, channel pitch %s)",
             target, len(world_rows), tuple(data["channel_pitch"]))
    return target


def layout_from_points(points: dict[str, tuple[int, int]],
                       client_size: tuple[int, int]) -> ReconnectLayout:
    """Build a layout from the five recorded points.

    Raises ValueError when the points cannot describe the lists (identical rows, no pitch).
    """

    try:
        world_0 = tuple(int(v) for v in points["world_row_0"])
        world_1 = tuple(int(v) for v in points["world_row_1"])
        channel_1 = tuple(int(v) for v in points["channel_1"])
        channel_2 = tuple(int(v) for v in points["channel_2"])
        channel_6 = tuple(int(v) for v in points["channel_6"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"missing calibration point: {exc}") from exc
    row_pitch = world_1[1] - world_0[1]
    if row_pitch <= 0:
        raise ValueError("世界列表两行必须在不同高度（第 2 行应在第 1 行下面）")
    pitch_x = channel_2[0] - channel_1[0]
    if pitch_x <= 0:
        raise ValueError("第 2 个频道必须在第 1 个频道右侧")
    pitch_y = channel_6[1] - channel_1[1]
    if pitch_y <= 0:
        raise ValueError("第 6 个频道必须在下一行（应在第 1 个频道下方）")
    return ReconnectLayout(
        reference_client=(int(client_size[0]), int(client_size[1])),
        world_first_row_centre=(world_0[0], world_0[1]),
        world_row_pitch=int(row_pitch),
        first_channel_centre=(channel_1[0], channel_1[1]),
        channel_pitch=(int(pitch_x), int(pitch_y)),
        channels_per_row=CHANNELS_PER_ROW,
    )


def load_layout(path: Optional[Path] = None) -> ReconnectLayout:
    """The calibrated layout, or the built-in defaults when there is none.

    The geometry is measured once on the operator's own screenshots (the two select
    windows are fixed art), so it is stored as JSON instead of being hard-coded: the
    calibration tool writes the eight points the operator clicks, and this reads them.
    A broken or missing file simply falls back to the defaults.
    """

    import json

    target = Path(path) if path is not None else layout_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RECONNECT_LAYOUT
    try:
        world_rows = [tuple(int(v) for v in point) for point in data["world_rows"]]
        first_channel = tuple(int(v) for v in data["first_channel_centre"])
        pitch = tuple(int(v) for v in data["channel_pitch"])
        per_row = int(data["channels_per_row"])
    except (KeyError, TypeError, ValueError):
        LOG.warning("auto reconnect: layout file %s is unusable; using defaults", target)
        return RECONNECT_LAYOUT
    if len(world_rows) < 2 or world_rows[1][1] <= world_rows[0][1]:
        LOG.warning("auto reconnect: layout file %s has no row pitch; using defaults",
                    target)
        return RECONNECT_LAYOUT
    return ReconnectLayout(
        reference_client=tuple(
            int(v) for v in data.get("reference_client", (1366, 768))
        ),
        world_first_row_centre=world_rows[0],
        world_row_pitch=int(world_rows[1][1] - world_rows[0][1]),
        first_channel_centre=first_channel,
        channel_pitch=pitch,
        channels_per_row=max(1, per_row),
    )


def valid_channel(value: Any) -> Optional[int]:
    """The channel number for a user entry, or None when it is not 1..60."""

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if CHANNEL_MIN <= number <= CHANNEL_MAX:
        return number
    return None


@dataclass(frozen=True)
class LoginColourReference:
    """The login page's base colour and the narrow range around it.

    ``login_page_target.jpg`` is a crop of the page's cream background.  Its pixels
    are therefore not a shape to correlate but the definition of what the page looks like:
    the base colour (per-channel median) plus the range that holds the crop's own pixels
    (1st-99th percentile, widened by a margin).  That keeps the crop's texture - a page that
    only matched one flat value would miss most of its own background.
    """

    base_bgr: tuple[int, int, int]
    lower_bgr: tuple[int, int, int]
    upper_bgr: tuple[int, int, int]
    pixels: int

    def describe(self) -> str:
        return (f"base BGR {self.base_bgr}, range {self.lower_bgr}..{self.upper_bgr} "
                f"(from {self.pixels} reference pixels)")


@dataclass(frozen=True)
class LoginPageEvidence:
    """How much of the game window is the login page's colour."""

    fraction: float          # largest single same-colour region / window pixels
    area: int                # its pixel count
    bbox: tuple[int, int, int, int]
    coverage: float          # every in-range pixel / window pixels (diagnostics)

    def describe(self) -> str:
        return (f"largest same-colour region {self.fraction * 100:.1f}% "
                f"({self.area} px at {self.bbox}), in range overall "
                f"{self.coverage * 100:.1f}%")


def login_colour_reference(image) -> Optional[LoginColourReference]:
    """The base colour and range of a login-page reference crop (BGR)."""

    import numpy as np

    if image is None:
        return None
    pixels = int(image.shape[0] * image.shape[1])
    if pixels <= 0:
        return None
    base = tuple(int(np.median(image[:, :, index])) for index in range(3))
    lower = tuple(
        int(max(0, np.percentile(image[:, :, index], LOGIN_COLOUR_PERCENTILE_LOW)
                - LOGIN_COLOUR_MARGIN))
        for index in range(3)
    )
    upper = tuple(
        int(min(255, np.percentile(image[:, :, index], LOGIN_COLOUR_PERCENTILE_HIGH)
                + LOGIN_COLOUR_MARGIN))
        for index in range(3)
    )
    return LoginColourReference(base, lower, upper, pixels)


def load_login_reference(path: Optional[Path] = None) -> Optional[LoginColourReference]:
    """The colour reference from the login-page crop (None when unusable).

    Without an explicit ``path`` the file is searched in LOGIN_REFERENCE_PATHS: the personal
    screenshots folder first, then the recording-assets copy that ships with the package.
    """

    import cv2

    if path is not None:
        candidates = (Path(path),)
    else:
        candidates = tuple(Path(entry) for entry in LOGIN_REFERENCE_PATHS)
    for reference_path in candidates:
        if not reference_path.is_file():
            continue
        image = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
        if image is None:
            LOG.warning("auto reconnect: login page colour reference unreadable: %s",
                        reference_path)
            continue
        reference = login_colour_reference(image)
        if reference is None:
            LOG.warning("auto reconnect: login page colour reference is empty: %s",
                        reference_path)
            continue
        LOG.info("auto reconnect: login page colour reference from %s (%s)",
                 reference_path, reference.describe())
        return reference
    LOG.warning("auto reconnect: no login page colour reference found in %s",
                ", ".join(str(entry) for entry in candidates))
    return None


def login_colour_mask(frame, reference: LoginColourReference):
    """The pixels of ``frame`` that are inside the reference's colour range."""

    import cv2
    import numpy as np

    if frame is None or reference is None:
        return None
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    mask = cv2.inRange(
        frame,
        np.array(reference.lower_bgr, dtype=np.uint8),
        np.array(reference.upper_bgr, dtype=np.uint8),
    )
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def analyse_login_page(frame, reference: LoginColourReference) -> Optional[LoginPageEvidence]:
    """Measure the same-colour area of ``frame`` (largest single region + coverage)."""

    import cv2
    import numpy as np

    mask = login_colour_mask(frame, reference)
    if mask is None:
        return None
    total = int(mask.shape[0] * mask.shape[1])
    if total <= 0:
        return None
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return LoginPageEvidence(0.0, 0, (0, 0, 0, 0), 0.0)
    areas = stats[1:, cv2.CC_STAT_AREA]
    best = int(np.argmax(areas)) + 1
    area = int(stats[best, cv2.CC_STAT_AREA])
    bbox = (int(stats[best, cv2.CC_STAT_LEFT]), int(stats[best, cv2.CC_STAT_TOP]),
            int(stats[best, cv2.CC_STAT_WIDTH]), int(stats[best, cv2.CC_STAT_HEIGHT]))
    coverage = float((mask > 0).sum()) / total
    return LoginPageEvidence(float(area) / total, area, bbox, coverage)


def find_login_page(
    frame,
    reference: LoginColourReference,
    *,
    min_fraction: float = LOGIN_PAGE_MIN_FRACTION,
    min_pixels: int = LOGIN_PAGE_MIN_PIXELS,
) -> Optional[LoginPageEvidence]:
    """The login-page evidence when ``frame`` shows it, else None.

    ``frame`` is the BGR capture of the whole game window (the 1366x768 client preset).
    A page counts as the login page when ONE single region of its base colour covers at
    least ``min_fraction`` of the window (and at least ``min_pixels`` pixels).
    """

    evidence = analyse_login_page(frame, reference)
    if evidence is None:
        return None
    if evidence.area < int(min_pixels) or evidence.fraction < float(min_fraction):
        return None
    return evidence


class ScreenClicker:
    """Send a real left click at a screen position (no dependency on the aim module)."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = bool(dry_run)
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> bool:
        self.clicks.append((int(x), int(y)))
        if self.dry_run:
            LOG.info("auto reconnect: DRY-RUN click at (%d, %d)", x, y)
            return True
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
            time.sleep(0.03)
            user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP
            return True
        except Exception:
            LOG.exception("auto reconnect: click at (%d, %d) failed", x, y)
            return False


class ReconnectWorker(threading.Thread):
    """Watch for a confirmed disconnect and log the character back in."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        result_queue: "queue.Queue[tuple[str, str]]",
        *,
        window_title: str = "",
        layout: Optional[ReconnectLayout] = None,
        capture_fn: Optional[Callable[[], Any]] = None,
        clicker: Any = None,
        template_path: Optional[Path] = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        client_size_fn: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
    ) -> None:
        super().__init__(name="auto-reconnect-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.result_queue = result_queue
        self.window_title = str(window_title)
        self.layout = layout if layout is not None else load_layout()
        self.clicker = clicker if clicker is not None else ScreenClicker(dry_run=dry_run)
        self.dry_run = bool(dry_run)
        self._sleep = sleep
        self._capture_fn = capture_fn
        self._template_path = template_path
        self._client_size_fn = client_size_fn
        self._reference: Optional[LoginColourReference] = None
        self._reference_loaded = False

        self._lock = threading.Lock()
        self._enabled = False
        self._world = WORLD_DEFAULT
        self._channel = CHANNEL_DEFAULT
        self._wake = threading.Event()
        self._login_entered = False
        # Set by trigger_test(): the temporary 测试重连 button runs the sequence once even
        # when the enable checkbox is still off, so the operator can try it before trusting
        # the automatic 掉线 trigger.
        self._test_requested = False
        self._testing = False
        # Points the operator records from the panel (see CALIBRATION_STEPS).
        self._calibration: dict[str, tuple[int, int]] = {}
        self._calibration_client: Optional[tuple[int, int]] = None
        self._calibration_complete = False

    # ------------------------------------------------------------------ settings

    def set_enabled(self, enabled: bool) -> None:
        """Arm or disarm the worker - ticking the box never starts a run.

        The operator's rule: the selection only makes the worker ready.  The drill starts on
        the 掉线 event or when the 测试重连 button is clicked.  (Waking the loop from here once
        ran the whole sequence - Enter and the two clicks included - the moment the box was
        ticked.)
        """

        with self._lock:
            self._enabled = bool(enabled)
            self._login_entered = False
        LOG.info("auto reconnect %s", "enabled" if enabled else "disabled")

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_world(self, world: str) -> bool:
        """Pick one of the five worlds; False when the name is unknown."""

        name = str(world).strip()
        if name not in WORLD_NAMES:
            LOG.warning("auto reconnect: unknown world %r", world)
            return False
        with self._lock:
            self._world = name
        return True

    def set_channel(self, value: Any) -> bool:
        """Set the channel; only an integer 1..60 is accepted."""

        number = valid_channel(value)
        if number is None:
            LOG.warning("auto reconnect: channel %r is not an integer 1-%d",
                        value, CHANNEL_MAX)
            return False
        with self._lock:
            self._channel = number
        return True

    def settings(self) -> tuple[bool, str, int]:
        with self._lock:
            return self._enabled, self._world, self._channel

    # ------------------------------------------------------------------- triggers

    def notify_disconnect(self) -> None:
        """The 掉线 event happened (first sign) - check for the login page."""

        with self._lock:
            if not self._enabled or self._login_entered:
                return
        LOG.info("auto reconnect: disconnect event seen; checking the game window")
        self._wake.set()

    def trigger_test(self) -> bool:
        """Run the whole sequence once from the panel's temporary 测试重连 button.

        The button exists to try the function *before* trusting it: the enable checkbox is
        therefore ignored for this run, and a finished run re-arms so the button can be
        pressed again.  False means a run is already going on.
        """

        with self._lock:
            if self._test_requested or self._testing:
                LOG.info("auto reconnect: a manual test is already queued/running")
                return False
            self._test_requested = True
            self._testing = True
            # a previous automatic run must not block this one
            self._login_entered = False
        LOG.info("auto reconnect: manual test requested from the panel")
        self._report("test", "手动测试")
        self._wake.set()
        return True

    def _take_test_request(self) -> bool:
        with self._lock:
            requested = self._test_requested
            self._test_requested = False
            return requested

    def _report(self, state: str, detail: str = "") -> None:
        """Report one step to the panel - and log every failure as an ERROR.

        error.log only receives ERROR and above (see assistant.py), so a run that stops must
        say so at ERROR level: "Enter was not sent" was invisible in error.log before this,
        which made the failure impossible to diagnose from the log file alone.
        """

        if state == "failed":
            LOG.error("auto reconnect failed: %s", detail or "(no detail)")
        else:
            LOG.info("auto reconnect %s%s", state, f": {detail}" if detail else "")
        try:
            self.result_queue.put_nowait((state, detail))
        except queue.Full:
            LOG.warning("auto reconnect result dropped: %s", state)

    # ---------------------------------------------------------------------- loop

    def run(self) -> None:
        LOG.info("auto reconnect worker started (login colour reference %s)",
                 self._template_path or SCREENSHOTS_DIR / LOGIN_TEMPLATE_NAME)
        try:
            while not self.stop_event.is_set():
                if not self._wake.wait(0.25):
                    continue
                self._wake.clear()
                if self.stop_event.is_set():
                    break
                try:
                    self._handle_disconnect()
                except Exception as exc:
                    LOG.exception("auto reconnect failed")
                    self._report("failed", str(exc))
        finally:
            LOG.info("auto reconnect worker stopped")

    def _handle_disconnect(self) -> None:
        enabled, world, channel = self.settings()
        if not enabled and not self._test_requested:
            return
        testing = self._take_test_request()
        try:
            self._run_sequence(world, channel, testing)
        finally:
            with self._lock:
                self._testing = False

    def _run_sequence(self, world: str, channel: int, testing: bool) -> None:
        if testing:
            LOG.info("auto reconnect: manual test run (ignoring the enable checkbox)")
        if not self._prepare_window():
            self._report("failed", "game window is not available")
            return
        self._report("checking", f"{world} {channel}频道")
        if testing:
            # The button is clicked deliberately while the operator looks at the login page,
            # so the colour gate does not BLOCK the drill - it is still measured and reported,
            # because that number is what the gate is calibrated with.  The automatic 掉线
            # path always waits for it.
            LOG.info("auto reconnect: manual test - measuring the login-page colour only")
            self._report_login_colour()
        elif not self._wait_for_login_page():
            # _wait_for_login_page reported the precise reason (missing reference / measured
            # colour area); a second, vaguer failure line would only hide it.
            return
        if not self.layout.is_calibrated:
            # Measured in the field: with the all-zero default layout the clicks went to the
            # window's top-left corner ("client (0, 0)"), so nothing was selected and the run
            # still reported success.  Refuse instead, and say what is missing.
            LOG.error("auto reconnect: the select-window geometry is not calibrated (%s) - "
                      "refusing to click; measured world rows/channel grid needed",
                      layout_path())
            self._report(
                "failed",
                "选择窗口未标定（世界行/频道位置未知）：请在游戏位于世界列表、频道列表时各按一次"
                "「截取游戏窗口」，或运行 work\\reconnect_calibrate.py 生成 reconnect_layout.json",
            )
            return
        # From here the sequence really sends input: arm live input if the operator has not
        # (the default), and put that state back when the sequence ends either way.
        previous_input = self._arm_input()
        try:
            self._report("login-page", "pressing Enter")
            reason = self._press("enter")
            if reason:
                self._report("failed", reason)
                return
            if not self._sleep_checked(LOGIN_WAIT_SECONDS):
                return
            if not self._select_world(world):
                return
            if not self._select_channel(channel):
                return
            with self._lock:
                self._login_entered = True
            self._report("done", f"{world} {channel}频道")
        finally:
            self._restore_input(previous_input)

    # ---------------------------------------------------------------- calibration

    def calibration_progress(self) -> tuple[int, int]:
        with self._lock:
            if self._calibration_complete:
                return len(CALIBRATION_STEPS), len(CALIBRATION_STEPS)
            return len(self._calibration), len(CALIBRATION_STEPS)

    def calibration_next_step(self) -> Optional[tuple[str, str]]:
        """The next point to record, or None when the calibration is finished.

        A *finished* calibration must not look like an empty one (both have no pending points),
        otherwise the panel would silently start over instead of saying it is done.
        """

        with self._lock:
            if self._calibration_complete:
                return None
            recorded = dict(self._calibration)
        for key, label in CALIBRATION_STEPS:
            if key not in recorded:
                return key, label
        return None

    def calibration_points(self) -> dict[str, tuple[int, int]]:
        with self._lock:
            return dict(self._calibration)

    def calibrated(self) -> bool:
        with self._lock:
            return self._calibration_complete

    def reset_calibration(self) -> None:
        """Forget the recorded points so the five steps can be taken again."""

        with self._lock:
            self._calibration.clear()
            self._calibration_client = None
            self._calibration_complete = False
        LOG.info("auto reconnect: calibration points cleared")

    def record_layout_point(
        self, step: str, screen_point: Optional[tuple[int, int]] = None,
        path: Optional[Path] = None,
    ) -> tuple[bool, str]:
        """Record one calibration point (the mouse position over that game entry).

        The point is stored in client coordinates of the captured game window, so the stored
        geometry does not depend on where the window sits on the desktop.  When the fifth
        point arrives the layout is validated, written to reconnect_layout.json and used
        immediately.
        """

        labels = dict(CALIBRATION_STEPS)
        if step not in labels:
            return False, f"未知的标定步骤 {step}"
        frame, rect = self._capture()
        if frame is None:
            LOG.error("auto reconnect: calibration needs the game window, none captured")
            return False, "无法截取游戏窗口（请先让游戏窗口可见）"
        if screen_point is None:
            try:
                screen_point = cursor_screen_position()
            except Exception:
                LOG.exception("auto reconnect: cannot read the cursor position")
                return False, "无法读取鼠标位置"
        client_size = (int(frame.shape[1]), int(frame.shape[0]))
        point = (int(screen_point[0]) - int(rect[0]), int(screen_point[1]) - int(rect[1]))
        if not (0 <= point[0] < client_size[0] and 0 <= point[1] < client_size[1]):
            LOG.error("auto reconnect: calibration point %s is outside the game window "
                      "(client %s, size %s)", labels[step], point, client_size)
            return False, f"鼠标不在游戏窗口内（客户端坐标 {point}）"
        with self._lock:
            self._calibration[step] = point
            self._calibration_client = client_size
            self._calibration_complete = False
            recorded = dict(self._calibration)
        LOG.info("auto reconnect: calibration point %s = client %s", labels[step], point)
        if len(recorded) < len(CALIBRATION_STEPS):
            self._report("calibrate", f"{labels[step]} = {point[0]},{point[1]}")
            return True, f"已记录 {labels[step]}（{point[0]},{point[1]}）"
        try:
            layout = layout_from_points(recorded, client_size)
        except ValueError as exc:
            LOG.error("auto reconnect: calibration rejected: %s", exc)
            with self._lock:
                self._calibration.clear()
            self._report("failed", f"标定失败：{exc}")
            return False, str(exc)
        try:
            written = write_layout(layout, path)
        except OSError:
            LOG.exception("auto reconnect: layout could not be written")
            return False, "标定结果无法写入 reconnect_layout.json"
        self.layout = layout
        with self._lock:
            self._calibration.clear()
            self._calibration_client = None
            self._calibration_complete = True
        summary = (f"标定完成：世界行距 {layout.world_row_pitch}px，"
                   f"频道间距 {layout.channel_pitch[0]}x{layout.channel_pitch[1]}px，"
                   f"参考客户端 {layout.reference_client[0]}x{layout.reference_client[1]}"
                   f"（{written.name}）")
        LOG.info("auto reconnect: %s", summary)
        self._report("calibrated", summary)
        return True, summary

    def save_diagnostic_capture(
        self, prefix: str = "select_window", folder: Optional[Path] = None
    ) -> Optional[Path]:
        """Save one frame of the game window for calibration/diagnosis.

        The geometry (world rows, channel grid) has to be measured from a real frame, and the
        game runs on the operator's own machine - this is how such a frame is produced without
        a command line.  Returns the written path, or None when the window could not be
        captured.
        """

        frame, _rect = self._capture()
        if frame is None:
            LOG.error("auto reconnect: diagnostic capture failed - no game window frame")
            return None
        import cv2

        target_folder = Path(folder) if folder is not None else SCREENSHOTS_DIR
        path = target_folder / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        # JPG, not PNG: this is a diagnostic dump (image_io holds the format policy).
        saved = save_screenshot(path, frame)
        if saved is None:
            LOG.error("auto reconnect: diagnostic capture could not be written to %s", path)
            return None
        LOG.info("auto reconnect: diagnostic capture saved to %s (%dx%d)", saved,
                 frame.shape[1], frame.shape[0])
        self._report("capture", str(saved))
        return saved

    def measure_login_page_colour(self) -> Optional[LoginPageEvidence]:
        """How much of the game window is the login page's colour right now."""

        reference = self._login_reference()
        if reference is None:
            return None
        frame, _rect = self._capture()
        return analyse_login_page(frame, reference)

    def _report_login_colour(self) -> None:
        """Report the measured colour area (the number the gate is calibrated with)."""

        evidence = self.measure_login_page_colour()
        if evidence is None:
            self._report("colour", "没有可用的颜色参考图")
            return
        matched = (evidence.fraction >= LOGIN_PAGE_MIN_FRACTION
                   and evidence.area >= LOGIN_PAGE_MIN_PIXELS)
        LOG.info("auto reconnect: login page colour measurement: %s -> %s (threshold "
                 "%.1f%% of the window)", evidence.describe(),
                 "login page" if matched else "not the login page",
                 LOGIN_PAGE_MIN_FRACTION * 100.0)
        self._report(
            "colour",
            f"同色区域 {evidence.fraction * 100:.1f}%（阈值 {LOGIN_PAGE_MIN_FRACTION * 100:.1f}%）"
            + ("，判定为登录页" if matched else "，判定为非登录页"),
        )

    def _prepare_window(self) -> bool:
        """Bring the game window forward so clicks and keys reach it.

        Returns False with the reason reported, so "game window is not available" never hides
        which of the two steps failed.
        """

        sender = self.key_sender
        if sender is None:
            self._report("failed", "没有按键发送器")
            return False
        try:
            selected = sender.select_window()
        except Exception:
            LOG.exception("auto reconnect: window selection failed")
            self._report("failed", "选中游戏窗口时出错")
            return False
        if selected is False:
            LOG.error("auto reconnect: the game window was not selected (%r)", self.window_title)
            self._report("failed", f"找不到游戏窗口 {self.window_title or '(title)'}")
            return False
        if not self._window_is_foreground():
            LOG.error("auto reconnect: the game window did not come to the foreground")
            self._report("failed", "游戏窗口没有切到前台")
            return False
        return True

    def _wait_for_login_page(self) -> bool:
        """Second sign: the game window must be the login page's base colour.

        The reference crop defines the colour to look for; a frame counts as the login page
        when one single region of that colour covers at least LOGIN_PAGE_MIN_FRACTION of the
        window (measured: the select windows reach 11.7 %, in-game frames 0.0-0.1 %).  The
        measured value is logged either way, so the range can be tuned from the log alone.
        """

        reference = self._login_reference()
        if reference is None:
            LOG.error("auto reconnect: no usable login page colour reference (%s)",
                      self._template_path or " then ".join(
                          str(entry) for entry in LOGIN_REFERENCE_PATHS))
            self._report("failed", f"找不到登录页颜色参考图 {LOGIN_TEMPLATE_NAME}")
            return False
        LOG.info("auto reconnect: login page colour reference %s", reference.describe())
        best: Optional[LoginPageEvidence] = None
        captured = 0
        for attempt in range(1, max(1, LOGIN_CHECK_ATTEMPTS) + 1):
            frame, _rect = self._capture()
            evidence = analyse_login_page(frame, reference) if frame is not None else None
            if frame is not None:
                captured += 1
            if evidence is not None and (best is None or evidence.fraction > best.fraction):
                best = evidence
            if evidence is not None and find_login_page(frame, reference) is not None:
                LOG.info("auto reconnect: login page colour found on attempt %d (%s)",
                         attempt, evidence.describe())
                return True
            if self._sleep is not time.sleep:
                self._sleep(LOGIN_CHECK_INTERVAL_SECONDS)
                if self.stop_event.is_set():
                    return False
            elif self.stop_event.wait(LOGIN_CHECK_INTERVAL_SECONDS):
                return False
        if captured == 0:
            LOG.error("auto reconnect: the game window could not be captured at all "
                      "(%d attempts)", max(1, LOGIN_CHECK_ATTEMPTS))
            self._report("failed", "无法截取游戏窗口（无法确认登录页）")
            return False
        measured = best.describe() if best is not None else "同色区域 0.0%"
        LOG.error("auto reconnect: the game window does not show the login page colour "
                  "(%s; need one region >= %.1f%% of the window)",
                  measured, LOGIN_PAGE_MIN_FRACTION * 100.0)
        self._report("failed", f"游戏窗口不是登录页（{measured}）")
        return False

    def _select_world(self, world: str) -> bool:
        """First select window: click the row of the chosen world."""

        index = WORLD_NAMES.index(world)
        point = self.layout.world_click_point(index, self._client_size())
        frame, rect = self._capture()
        if frame is None:
            self._report("failed", "无法截取游戏窗口（第一选择窗口）")
            return False
        screen_x = rect[0] + point[0]
        screen_y = rect[1] + point[1]
        LOG.info("auto reconnect: clicking world %s (row %d) at client (%d, %d) = "
                 "screen (%d, %d)", world, index + 1, point[0], point[1],
                 screen_x, screen_y)
        if not self.clicker.click(screen_x, screen_y):
            LOG.error("auto reconnect: the click on world %s (row %d) failed", world, index + 1)
            self._report("failed", f"点击 {world}（第 {index + 1} 行）失败")
            return False
        self._report("world", f"{world} (row {index + 1})")
        return self._sleep_checked(SELECT_WINDOW_WAIT_SECONDS)

    def _select_channel(self, channel: int) -> bool:
        """Second select window: click channel 1, then move to the target with the keys."""

        first = self.layout.channel_click_point(CHANNEL_MIN, self._client_size())
        frame, rect = self._capture()
        if frame is None:
            self._report("failed", "无法截取游戏窗口（第二选择窗口）")
            return False
        screen_x = rect[0] + first[0]
        screen_y = rect[1] + first[1]
        LOG.info("auto reconnect: clicking the first channel at client (%d, %d) = "
                 "screen (%d, %d)", first[0], first[1], screen_x, screen_y)
        if not self.clicker.click(screen_x, screen_y):
            LOG.error("auto reconnect: the click on the first channel failed")
            self._report("failed", "点击第一频道失败")
            return False
        self._report("channel", "第一频道")
        if not self._sleep_checked(SELECT_WINDOW_WAIT_SECONDS):
            return False
        moves = self.layout.channel_key_moves(channel)
        LOG.info("auto reconnect: moving to channel %d with %s", channel,
                 " ".join(moves) if moves else "(no move)")
        for key in moves:
            if self.stop_event.is_set():
                return False
            reason = self._press(key)
            if reason:
                self._report("failed", reason)
                return False
            time.sleep(KEY_DELAY_SECONDS)
        reason = self._press("enter")
        if reason:
            self._report("failed", reason)
            return False
        self._report("enter", f"{channel}频道")
        if not self._sleep_checked(CHANNEL_CONFIRM_WAIT_SECONDS):
            return False
        # second Enter: the operator's sequence is ... Enter -> 2s -> Enter
        reason = self._press("enter")
        if reason:
            self._report("failed", reason)
            return False
        self._report("enter-confirm", f"{channel}频道")
        return self._sleep_checked(LOGIN_SETTLE_SECONDS)

    # ------------------------------------------------------------------ plumbing

    def _login_reference(self) -> Optional[LoginColourReference]:
        if not self._reference_loaded:
            self._reference = load_login_reference(self._template_path)
            self._reference_loaded = True
        return self._reference

    def reload_login_reference(self) -> Optional[LoginColourReference]:
        """Use screenshots/login_page_target.jpg again (after it was replaced)."""

        self._reference_loaded = False
        self._reference = None
        return self._login_reference()

    def _capture(self):
        """One capture of the whole game window -> (BGR frame, screen rect)."""

        if self._capture_fn is not None:
            frame, rect = self._capture_fn()
            return frame, rect
        try:
            from capture_worker import capture_window

            image, rect = capture_window(self.window_title)
        except Exception:
            LOG.warning("auto reconnect: game window capture failed", exc_info=True)
            return None, (0, 0, 0, 0)
        import numpy as np

        return np.asarray(image)[:, :, ::-1].copy(), rect
        # capture_window returns RGB (PIL); OpenCV templates are BGR/BGR-gray, so the
        # channel order is flipped back here.

    def _client_size(self) -> tuple[int, int]:
        if self._client_size_fn is not None:
            size = self._client_size_fn()
            if size:
                return (int(size[0]), int(size[1]))
        frame, _rect = self._capture()
        if frame is None:
            return self.layout.reference_client
        return (int(frame.shape[1]), int(frame.shape[0]))

    def _press(self, key: str) -> Optional[str]:
        """Press one key - None when it was delivered, else the reason it was not.

        A bare "not claimed" was useless: the sender refuses a key while live input is
        disarmed (the default until 开始巡逻) or while the game window is not foreground, and
        neither reason could be told apart from the old message.  The reason is returned so
        the caller reports exactly one failure.
        """

        sender = self.key_sender
        if sender is None:
            return "没有按键发送器"
        state = self._input_state()
        if state is False:
            LOG.error("auto reconnect: live input is disarmed - key %s was not sent", key)
            return "实时输入未开启（请点击 开始巡逻 启用输入后再试）"
        if not self._window_is_foreground():
            LOG.error("auto reconnect: game window is not foreground - key %s was not sent",
                      key)
            return "游戏窗口不在前台，按键被拒绝"
        try:
            ok = sender.press(key, duration=KEY_HOLD_SECONDS)
        except Exception:
            LOG.exception("auto reconnect: key %s failed", key)
            return f"按键 {key} 发送异常"
        if not ok:
            LOG.error("auto reconnect: key %s was not claimed (input armed=%s, foreground=%s)",
                      key, state, True)
            return f"按键 {key} 未被接受（游戏窗口没有接管键盘）"
        return None

    # ------------------------------------------------------------------ live input

    def _input_state(self) -> Optional[bool]:
        """Whether live keyboard input is armed, or None when the sender has no such API."""

        check = getattr(self.key_sender, "input_is_enabled", None)
        if not callable(check):
            return None
        try:
            return bool(check())
        except Exception:
            LOG.debug("auto reconnect: input state is unavailable", exc_info=True)
            return None

    def _window_is_foreground(self) -> bool:
        check = getattr(self.key_sender, "is_game_foreground", None)
        if not callable(check):
            return True
        try:
            return bool(check())
        except Exception:
            LOG.debug("auto reconnect: foreground check failed", exc_info=True)
            return True

    def _arm_input(self) -> Optional[bool]:
        """Enable live input for this sequence -> the previous state.

        The keys cannot be delivered while the assistant's live input is disarmed, which is
        the default until 开始巡逻/Start Patrol - that is exactly how the first field test
        ended with ``key enter was not claimed``.  A reconnect is an explicit recovery action,
        so it arms input for its own sequence and restores the previous state afterwards.
        Returns None when the sender has no such API (tests, other senders).
        """

        previous = self._input_state()
        if previous is None or previous:
            return previous
        enable = getattr(self.key_sender, "enable_input", None)
        if not callable(enable):
            return previous
        try:
            enable()
        except Exception:
            LOG.exception("auto reconnect: live input could not be armed")
            self._report("failed", "无法开启实时输入")
            return previous
        LOG.info("auto reconnect: live input armed for this sequence (it was disarmed)")
        self._report("input", "已临时开启实时输入（序列结束后恢复）")
        return previous

    def _restore_input(self, previous: Optional[bool]) -> None:
        """Put the live-input state back the way the sequence found it."""

        if previous is None or previous:
            return
        disable = getattr(self.key_sender, "disable_input", None)
        if not callable(disable):
            return
        try:
            disable()
            LOG.info("auto reconnect: live input disarmed again (as it was before)")
        except Exception:
            LOG.exception("auto reconnect: live input could not be disarmed again")

    def _sleep_checked(self, seconds: float) -> bool:
        """Sleep unless the assistant is stopping; False when it is.

        A caller-supplied clock (the tests) owns the waiting, so a test can assert the
        requested pauses without waiting for them.
        """

        if seconds <= 0:
            return True
        if self._sleep is not time.sleep:
            self._sleep(float(seconds))
            return not self.stop_event.is_set()
        return not self.stop_event.wait(float(seconds))


# ---------------------------------------------------------------------------
# Measured from the operator's own screenshots (screenshots/channel_select_first.jpg
# and channel_select_second.jpg) - see RECONNECT_LAYOUT.md next to this file.
RECONNECT_LAYOUT = ReconnectLayout()


__all__ = [
    "CALIBRATION_STEPS",
    "CHANNEL_MAX",
    "CHANNEL_MIN",
    "CHANNELS_PER_ROW",
    "LOGIN_PAGE_MIN_FRACTION",
    "LOGIN_TEMPLATE_NAME",
    "RECONNECT_LAYOUT",
    "LoginColourReference",
    "LoginPageEvidence",
    "ReconnectLayout",
    "ReconnectWorker",
    "ScreenClicker",
    "WORLD_NAMES",
    "analyse_login_page",
    "cursor_screen_position",
    "find_login_page",
    "layout_from_points",
    "layout_path",
    "load_layout",
    "load_login_reference",
    "login_colour_mask",
    "login_colour_reference",
    "valid_channel",
    "write_layout",
]
