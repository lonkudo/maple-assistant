"""Step 1 of the RoiTrack integration: game screenshot -> protocol frame payload.

This module is OURS.  The vendor material in this folder is READ-ONLY and must never be edited,
renamed or reformatted - `ct_lie3_remote_workflow_sample.py`, `2.7.0.md` and `ip_port.txt` are
reference copies (pinned by `test_autolie_api_reference.py`); our code goes here or into another
new file beside them.  See README.md, section "Reference material is read-only".

What this module does (and nothing else - no sockets, no session, no mouse):

    game window screenshot (BGR, client pixels)
        -> crop the precise lie ROI                  (310, 118, 745, 496) at 1366x768
        -> resize to the protocol ROI                372 x 248
        -> JPEG, quality 90                          3 channels, never PNG/gray
        -> base64                                    image_transport bgr_jpeg90_base64
        -> payload()                                 {"type":"frame","frame_id":..,"image":..}

    frame_result from the server ({"x":186.0,"y":124.0,...}, coordinates in the 372x248 ROI)
        -> ServerPoint.slot_xy()                     protocol ROI pixels
        -> ServerPoint.to_crop(geometry)             crop-local pixels (what the aim wants)
        -> ServerPoint.to_screen(geometry)           real cursor pixels

The measured geometry (operator measurement, 1366x768 reference client):

    lie popup preset (lie_geometry.lie_window_box)      (299,  85, 767, 598)
    precise lie ROI (this module)                       (310, 118, 745, 496)
      = the popup inset by 11 / 11 / 33 / 69 on left / right / top / bottom
      aspect 745:496 = 1.5021  vs the protocol ROI 372:248 = 1.5000  -> 0.14 %, no distortion
      the same box is the server's own lie3main space (x_main / y_main), so
      slot -> crop is a factor of 2.0027 horizontally and exactly 2.0 vertically.

The two spaces the server can answer in:

    x / y            protocol ROI (372x248)   -> default; scale by crop_w/372, crop_h/248
    x_main / y_main  lie3main   (745x496)     -> scale by crop_w/745, crop_h/496 (1:1 here)

Command line (previews written to work/):

    py -3.10 autolie_api\\intergration.py --demo screenshots\\channel_select_first.jpg
    py -3.10 autolie_api\\intergration.py --demo shot.png --point 186,124
    py -3.10 autolie_api\\intergration.py --box      # print the crop box for each client size
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                      # allow direct execution
    sys.path.insert(0, str(ROOT))

from image_io import SCREENSHOT_SUFFIX             # noqa: E402

LOG = logging.getLogger("api_auto_lie")

# --------------------------------------------------------------------------- protocol
# RoiTrack 2.7.0: ROI 372x248 BGR, JPEG q90; caps from handshake_ack.config.  Note that
# 53760 * 4 / 3 == 71680, i.e. the byte cap and the base64-char cap are the same limit.
SLOT_W = 372
SLOT_H = 248
JPEG_QUALITY = 90
MAX_FRAME_BYTES = 53760
MAX_FRAME_B64_CHARS = 71680

# --------------------------------------------------------------------------- geometry
# The server reports x_main / y_main in this space (lie3main).
LIE3MAIN_SIZE: Tuple[int, int] = (745, 496)
# The precise crop, measured by the operator on the 1366x768 client, and the popup preset it
# sits inside.  Expressed as insets so it follows the game-UI scale on other client sizes.
LIE_ROI_REFERENCE_CLIENT: Tuple[int, int] = (1366, 768)
LIE_ROI_REFERENCE_BOX: Tuple[int, int, int, int] = (310, 118, 745, 496)
LIE_ROI_INSET_LEFT = 11
LIE_ROI_INSET_RIGHT = 11
LIE_ROI_INSET_TOP = 33
LIE_ROI_INSET_BOTTOM = 69

# A crop this far from the 1.5 protocol aspect is centre-fitted instead of stretched.
ASPECT_TOLERANCE = 0.02
COORD_SPACE_ROI = "roi"
COORD_SPACE_MAIN = "main"
DEFAULT_FRAME_STANDARD = 5


def lie_roi_box(client_width: int, client_height: int) -> Tuple[int, int, int, int]:
    """The precise lie ROI for a client size -> (left, top, width, height) in client pixels.

    The popup is a game-UI preset that is always centred in the client
    (``lie_geometry.lie_window_box``); the ROI is that popup inset by 11/11/33/69, scaled
    like every other HUD measurement.  At the 1366x768 reference this returns exactly
    (310, 118, 745, 496).
    """

    from lie_geometry import lie_ui_scale, lie_window_box

    width = max(1, int(client_width))
    height = max(1, int(client_height))
    left, top, box_width, box_height = lie_window_box(width, height)
    scale = float(lie_ui_scale(width))
    inset_left = max(1, int(round(LIE_ROI_INSET_LEFT * scale)))
    inset_right = max(1, int(round(LIE_ROI_INSET_RIGHT * scale)))
    inset_top = max(1, int(round(LIE_ROI_INSET_TOP * scale)))
    inset_bottom = max(1, int(round(LIE_ROI_INSET_BOTTOM * scale)))
    return (
        left + inset_left,
        top + inset_top,
        max(2, box_width - inset_left - inset_right),
        max(2, box_height - inset_top - inset_bottom),
    )


def fit_slot(crop: np.ndarray) -> Tuple[int, int, int, int]:
    """Which part of ``crop`` maps onto the slot -> (x, y, width, height) inside the crop.

    The measured crop is already 1.5:1, so normally the whole crop is used (a pure scale, no
    distortion).  For a crop of a different aspect the middle is used instead of stretching it,
    because stretching would bend the geometry the server's answer has to be mapped through.
    """

    height, width = crop.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("empty crop")
    target = SLOT_W / float(SLOT_H)
    aspect = width / float(height)
    if abs(aspect - target) <= ASPECT_TOLERANCE * target:
        return (0, 0, int(width), int(height))
    if aspect > target:
        new_width = max(2, int(round(height * target)))
        return ((width - new_width) // 2, 0, new_width, int(height))
    new_height = max(2, int(round(width / target)))
    return (0, (height - new_height) // 2, int(width), new_height)


# --------------------------------------------------------------------------- geometry object
@dataclass(frozen=True)
class RoiGeometry:
    """Everything needed to convert between the server's ROI and our pixels.

    ``box`` is (left, top, width, height) in CLIENT pixels, ``window_rect`` is the client's
    (left, top, right, bottom) in SCREEN pixels, and ``content_box`` is the part of the crop
    that actually maps onto the slot (see :func:`fit_slot`).
    """

    box: Tuple[int, int, int, int]
    client_size: Tuple[int, int]
    window_rect: Tuple[int, int, int, int]
    content_box: Tuple[int, int, int, int] = (0, 0, 0, 0)

    def __post_init__(self) -> None:
        left, top, width, height = (int(value) for value in self.box)
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid crop box {self.box}")
        if self.content_box == (0, 0, 0, 0):
            object.__setattr__(self, "content_box", (0, 0, width, height))

    # ---- sizes -----------------------------------------------------------------
    @property
    def crop_size(self) -> Tuple[int, int]:
        return (int(self.box[2]), int(self.box[3]))

    @property
    def content_size(self) -> Tuple[int, int]:
        return (int(self.content_box[2]), int(self.content_box[3]))

    @property
    def scale_x(self) -> float:
        """Client (or screen) pixels per slot pixel, horizontally."""

        return self.content_size[0] / float(SLOT_W)

    @property
    def scale_y(self) -> float:
        return self.content_size[1] / float(SLOT_H)

    @property
    def main_scale_x(self) -> float:
        """Client pixels per lie3main pixel (1.0 when the crop IS lie3main)."""

        return self.content_size[0] / float(LIE3MAIN_SIZE[0])

    @property
    def main_scale_y(self) -> float:
        return self.content_size[1] / float(LIE3MAIN_SIZE[1])

    # ---- server -> us ----------------------------------------------------------
    def slot_to_crop_point(self, x: float, y: float) -> Tuple[float, float]:
        """372x248 ROI pixel -> crop-local pixel (what MouseAimController.push_target wants)."""

        content_left, content_top = self.content_box[0], self.content_box[1]
        return (content_left + float(x) * self.scale_x,
                content_top + float(y) * self.scale_y)

    def main_to_crop_point(self, x_main: float, y_main: float) -> Tuple[float, float]:
        """lie3main (745x496) pixel -> crop-local pixel."""

        content_left, content_top = self.content_box[0], self.content_box[1]
        return (content_left + float(x_main) * self.main_scale_x,
                content_top + float(y_main) * self.main_scale_y)

    def slot_to_client_point(self, x: float, y: float) -> Tuple[float, float]:
        """372x248 ROI pixel -> client pixel."""

        crop_x, crop_y = self.slot_to_crop_point(x, y)
        return (self.box[0] + crop_x, self.box[1] + crop_y)

    def slot_to_screen_point(self, x: float, y: float) -> Tuple[float, float]:
        """372x248 ROI pixel -> screen pixel (the real cursor position)."""

        client_x, client_y = self.slot_to_client_point(x, y)
        return (self.window_rect[0] + client_x, self.window_rect[1] + client_y)

    def main_to_screen_point(self, x_main: float, y_main: float) -> Tuple[float, float]:
        crop_x, crop_y = self.main_to_crop_point(x_main, y_main)
        return (self.window_rect[0] + self.box[0] + crop_x,
                self.window_rect[1] + self.box[1] + crop_y)

    # ---- us -> server (for tests and for drawing) -------------------------------
    def crop_to_slot_point(self, crop_x: float, crop_y: float) -> Tuple[float, float]:
        content_left, content_top = self.content_box[0], self.content_box[1]
        return ((float(crop_x) - content_left) / self.scale_x,
                (float(crop_y) - content_top) / self.scale_y)

    def screen_to_slot_point(self, screen_x: float, screen_y: float) -> Tuple[float, float]:
        return self.crop_to_slot_point(
            float(screen_x) - self.window_rect[0] - self.box[0],
            float(screen_y) - self.window_rect[1] - self.box[1],
        )

    # ---- screen rectangles -----------------------------------------------------
    def crop_screen_rect(self) -> Tuple[int, int, int, int]:
        """The crop's (left, top, right, bottom) in screen pixels - the aim region."""

        left = int(self.window_rect[0]) + int(self.box[0])
        top = int(self.window_rect[1]) + int(self.box[1])
        return (left, top, left + self.crop_size[0], top + self.crop_size[1])

    def describe(self) -> str:
        return (
            "box(l,t,w,h)={} client={}x{} window_rect={} slot_scale=({:.4f},{:.4f}) "
            "crop_screen_rect={}".format(
                self.box, self.client_size[0], self.client_size[1], self.window_rect,
                self.scale_x, self.scale_y, self.crop_screen_rect(),
            )
        )


# --------------------------------------------------------------------------- payload
@dataclass(frozen=True)
class SlotFrame:
    """The picture the server is sent, with the geometry that produced it."""

    jpeg: bytes
    base64_text: str
    geometry: RoiGeometry
    frame_id: int
    interval_sec: float
    encode_ms: float

    @property
    def byte_size(self) -> int:
        return len(self.jpeg)

    @property
    def base64_chars(self) -> int:
        return len(self.base64_text)

    def within_caps(self, *, max_bytes: int = MAX_FRAME_BYTES,
                    max_b64_chars: int = MAX_FRAME_B64_CHARS) -> Tuple[bool, str]:
        """(ok, reason) against the protocol caps (image_transport dependent)."""

        if self.byte_size > int(max_bytes):
            return False, (f"jpeg {self.byte_size} bytes > max_frame_bytes {int(max_bytes)}")
        if self.base64_chars > int(max_b64_chars):
            return False, (f"base64 {self.base64_chars} chars > max_frame_b64_chars "
                           f"{int(max_b64_chars)}")
        return True, ""

    def payload(self) -> dict:
        """The 2.7.0 base64 frame message (§5.1)."""

        return {
            "type": "frame",
            "frame_id": int(self.frame_id),
            "image": self.base64_text,
            "frame_interval_sec": round(float(self.interval_sec), 4),
        }

    def save(self, folder: Path) -> Optional[Path]:
        """Save exactly what is sent, named ``<frame_id>.jpg``.

        The name IS the protocol ``frame_id`` (2.7.0 §3: it starts at 1 for every round,
        increases by one, never skips and never repeats; ``round_end`` starts a new round and the
        numbering begins at 1 again).  A folder of these dumps therefore reads like the
        conversation - ``1.jpg``, ``2.jpg``, ``3.jpg`` - and each file is byte-identical to the
        JPEG that went to the server (no re-encode).
        """

        path = Path(folder) / f"{int(self.frame_id)}{SCREENSHOT_SUFFIX}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.jpeg)
        except OSError:
            LOG.error("api lie: could not save the sent frame to %s", path, exc_info=True)
            return None
        LOG.debug("api lie: sent frame %d saved to %s (%d bytes)", self.frame_id, path,
                  self.byte_size)
        return path

    def describe(self) -> str:
        return (f"frame_id={self.frame_id} jpeg={self.byte_size}B "
                f"({self.byte_size * 100.0 / MAX_FRAME_BYTES:.1f}% of cap) "
                f"base64={self.base64_chars} chars "
                f"({self.base64_chars * 100.0 / MAX_FRAME_B64_CHARS:.1f}% of cap) "
                f"encode={self.encode_ms:.1f}ms")


@dataclass(frozen=True)
class ServerPoint:
    """A parsed ``frame_result`` (WS text, HTTP body or mock dict)."""

    frame_id: int
    success: bool
    x: Optional[float]
    y: Optional[float]
    x_main: Optional[float]
    y_main: Optional[float]
    decision: str
    api_ok: Optional[bool]
    api_skip_reason: str
    timing_ms: Optional[float]
    quota_left: Optional[int]
    round_active: Optional[bool]
    error: str
    raw: Mapping[str, Any]
    # the rest of frame_result, kept because the logs and the panel show it
    mode: str = ""
    onnx_count: Optional[int] = None
    parsed_count: Optional[int] = None
    queue_depth: Optional[int] = None
    seconds_left: Optional[float] = None
    frames_in_round: Optional[int] = None

    def slot_xy(self) -> Tuple[float, float]:
        """The answer in protocol ROI pixels; raises when the server sent no point."""

        if self.x is None or self.y is None:
            raise ValueError("frame_result carries no x/y")
        return (float(self.x), float(self.y))

    def main_xy(self) -> Tuple[float, float]:
        if self.x_main is None or self.y_main is None:
            raise ValueError("frame_result carries no x_main/y_main")
        return (float(self.x_main), float(self.y_main))

    def is_hold(self) -> bool:
        """True when the answer is not a fresh sighting, so the last point must be kept.

        The doc names ``hold_prev`` and ``abandon_frame_hold`` for that, and
        ``freeze_search_only_static`` means the tracker stopped updating as well - none of the
        three is a new target position, so all of them hold.  Anything containing ``track`` is a
        real update.
        """

        lowered = self.decision.lower()
        return (not self.success) or "hold" in lowered or "freeze" in lowered

    def within_slot(self) -> bool:
        if self.x is None or self.y is None:
            return False
        return 0.0 <= float(self.x) < SLOT_W and 0.0 <= float(self.y) < SLOT_H

    def to_crop(self, geometry: RoiGeometry, *, space: str = COORD_SPACE_ROI) -> Tuple[float, float]:
        if space == COORD_SPACE_MAIN:
            return geometry.main_to_crop_point(*self.main_xy())
        return geometry.slot_to_crop_point(*self.slot_xy())

    def to_client(self, geometry: RoiGeometry,
                  *, space: str = COORD_SPACE_ROI) -> Tuple[float, float]:
        crop_x, crop_y = self.to_crop(geometry, space=space)
        return (geometry.box[0] + crop_x, geometry.box[1] + crop_y)

    def to_screen(self, geometry: RoiGeometry,
                  *, space: str = COORD_SPACE_ROI) -> Tuple[float, float]:
        client_x, client_y = self.to_client(geometry, space=space)
        return (geometry.window_rect[0] + client_x, geometry.window_rect[1] + client_y)

    def cross_check_spaces(self, tolerance_px: float = 2.0) -> bool:
        """True when x/y and x_main/y_main describe the same spot.

        The doc defines them as the same target in two spaces (372x248 and 745x496).  If they
        ever disagree by more than ``tolerance_px`` in main space, the two are not a simple
        scale of each other and the caller must pick one deliberately.
        """

        if self.x is None or self.y is None or self.x_main is None or self.y_main is None:
            return True
        expected_x = float(self.x) * LIE3MAIN_SIZE[0] / float(SLOT_W)
        expected_y = float(self.y) * LIE3MAIN_SIZE[1] / float(SLOT_H)
        return (abs(expected_x - float(self.x_main)) <= tolerance_px
                and abs(expected_y - float(self.y_main)) <= tolerance_px)

    def describe(self) -> str:
        return (f"frame_id={self.frame_id} success={self.success} decision={self.decision} "
                f"xy=({self.x},{self.y}) main=({self.x_main},{self.y_main}) "
                f"timing={self.timing_ms} quota_left={self.quota_left} error={self.error or '-'}")


# --------------------------------------------------------------------------- converters
def crop_region(client_bgr: np.ndarray,
                box: Optional[Tuple[int, int, int, int]] = None) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    """Cut the ROI out of a game-window screenshot -> (crop, clamped box).

    ``box`` is (left, top, width, height) in client pixels; ``None`` uses the precise measured
    ROI for this client size.  A box that reaches outside the frame is clamped, which keeps a
    different client size from raising mid-pass.
    """

    if client_bgr is None or client_bgr.size == 0:
        raise ValueError("no screenshot to crop")
    client_height, client_width = client_bgr.shape[:2]
    if box is None:
        box = lie_roi_box(client_width, client_height)
    left, top, width, height = (int(value) for value in box)
    left = max(0, min(left, max(0, client_width - 2)))
    top = max(0, min(top, max(0, client_height - 2)))
    width = max(2, min(width, client_width - left))
    height = max(2, min(height, client_height - top))
    clamped = (left, top, width, height)
    if clamped != tuple(int(value) for value in box):
        LOG.warning("api lie: ROI box %s clamped to %s for a %dx%d client",
                    tuple(box), clamped, client_width, client_height)
    return client_bgr[top:top + height, left:left + width].copy(), clamped


def resize_to_slot(content_bgr: np.ndarray) -> np.ndarray:
    """Resize the crop content to the protocol ROI (372x248).

    INTER_LINEAR matches the CT client's own converter; the measured crop is already 1.5:1, so
    there is no letterboxing and the mapping stays a pure scale.
    """

    if content_bgr.shape[0] == SLOT_H and content_bgr.shape[1] == SLOT_W:
        return content_bgr
    return cv2.resize(content_bgr, (SLOT_W, SLOT_H), interpolation=cv2.INTER_LINEAR)


def encode_slot_jpeg(slot_bgr: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    """JPEG-encode the slot (3 channels, quality 90) and check the file really is a JPEG."""

    if slot_bgr.ndim != 3 or slot_bgr.shape[2] != 3:
        raise ValueError("the protocol wants 3-channel BGR, not gray")
    if slot_bgr.shape[0] != SLOT_H or slot_bgr.shape[1] != SLOT_W:
        raise ValueError(f"slot must be {SLOT_W}x{SLOT_H}, got "
                         f"{slot_bgr.shape[1]}x{slot_bgr.shape[0]}")
    ok, buffer = cv2.imencode(".jpg", slot_bgr,
                              [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    data = buffer.tobytes()
    if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise RuntimeError("encoder did not produce a JPEG")
    return data


def to_base64(jpeg: bytes) -> str:
    """base64 (standard alphabet, padded) of the JPEG - image_transport bgr_jpeg90_base64."""

    return base64.b64encode(jpeg).decode("ascii")


def no_proxy_opener():
    """A urllib opener that ignores system proxies.

    Measured: with urllib's default opener each ``/v1/health`` call took **13 s** on this machine
    (a Windows registry proxy lookup stalling), while the raw-socket WS probe answered in 0.3 s.
    With proxies disabled the same call takes 0.11 s, so every HTTP call to the service uses this.
    """

    from urllib import request as urlrequest

    return urlrequest.build_opener(urlrequest.ProxyHandler({}))


def pack_rtf1(frame_id: int, meta: Mapping[str, Any], jpeg: bytes) -> bytes:
    """§5.2 RTF1 single binary packet - the recommended transport (no base64).

        [4B "RTF1"][4B frame_id][4B json_len][4B jpeg_len][json][jpeg]      (little endian)

    The metadata must not carry the image; the server reads the length fields instead.
    """

    import struct

    payload = json.dumps(dict(meta)).encode("utf-8")
    header = struct.pack("<4sIII", b"RTF1", int(frame_id), len(payload), len(jpeg))
    return header + payload + jpeg


def build_slot_frame(
    client_bgr: np.ndarray,
    box: Optional[Tuple[int, int, int, int]] = None,
    *,
    frame_id: int = 1,
    interval_sec: float = 1.0 / DEFAULT_FRAME_STANDARD,
    window_rect: Optional[Tuple[int, int, int, int]] = None,
    client_size: Optional[Tuple[int, int]] = None,
    quality: int = JPEG_QUALITY,
) -> SlotFrame:
    """Screenshot -> the frame payload the server expects, with its geometry attached.

    ``window_rect`` is where the client sits on the desktop (left, top, right, bottom); it is
    what turns the server's answer into a real cursor position.  Without it the client is
    assumed to start at (0, 0).
    """

    height, width = client_bgr.shape[:2]
    size = (int(client_size[0]), int(client_size[1])) if client_size else (width, height)
    rect = (tuple(int(v) for v in window_rect) if window_rect is not None
            else (0, 0, size[0], size[1]))
    crop, clamped = crop_region(client_bgr, box)
    content = fit_slot(crop)
    content_bgr = crop[content[1]:content[1] + content[3],
                       content[0]:content[0] + content[2]]
    geometry = RoiGeometry(box=clamped, client_size=size, window_rect=rect,
                           content_box=content)
    started = time.perf_counter()
    slot = resize_to_slot(content_bgr)
    jpeg = encode_slot_jpeg(slot, quality)
    encode_ms = (time.perf_counter() - started) * 1000.0
    return SlotFrame(
        jpeg=jpeg, base64_text=to_base64(jpeg), geometry=geometry,
        frame_id=int(frame_id), interval_sec=float(interval_sec), encode_ms=encode_ms,
    )


def parse_server_point(payload: Mapping[str, Any]) -> ServerPoint:
    """Normalise a ``frame_result`` (WS text / HTTP body / mock dict) into a ServerPoint."""

    data = dict(payload or {})
    error = data.get("error")

    def number(key: str) -> Optional[float]:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    timing = data.get("timing_ms")
    if isinstance(timing, Mapping):
        timing = timing.get("total")

    def integer(key: str) -> Optional[int]:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    session = data.get("session") if isinstance(data.get("session"), Mapping) else {}
    quota = data.get("quota_left", session.get("quota_left"))
    try:
        quota_left = int(quota) if quota is not None else None
    except (TypeError, ValueError):
        quota_left = None
    return ServerPoint(
        frame_id=int(data.get("frame_id") or 0),
        success=bool(data.get("success", False)),
        x=number("x"), y=number("y"),
        x_main=number("x_main"), y_main=number("y_main"),
        decision=str(data.get("decision") or ""),
        api_ok=data.get("api_ok") if isinstance(data.get("api_ok"), bool) else None,
        api_skip_reason=str(data.get("api_skip_reason") or ""),
        timing_ms=(float(timing) if isinstance(timing, (int, float)) else None),
        quota_left=quota_left,
        round_active=(data.get("round_active")
                      if isinstance(data.get("round_active"), bool) else None),
        error=str(error) if error else "",
        raw=data,
        mode=str(data.get("mode") or ""),
        onnx_count=integer("onnx_count"),
        parsed_count=integer("parsed_count"),
        queue_depth=integer("queue_depth"),
        seconds_left=number("seconds_left"),
        frames_in_round=integer("frames_in_round"),
    )


def check_caps(frame: SlotFrame, *, transport: str = "base64") -> Tuple[bool, str]:
    """Protocol caps for the chosen transport ("base64" uses the char cap as well)."""

    if transport == "binary":
        return frame.within_caps(max_b64_chars=1 << 30)
    return frame.within_caps()


# --------------------------------------------------------------------------- demo
def _annotate_source(client_bgr: np.ndarray, geometry: RoiGeometry,
                     screen_point: Optional[Tuple[float, float]]) -> np.ndarray:
    canvas = client_bgr.copy()
    left, top, width, height = geometry.box
    cv2.rectangle(canvas, (left, top), (left + width, top + height), (0, 255, 255), 2)
    content_x, content_y, content_w, content_h = geometry.content_box
    cv2.rectangle(canvas, (left + content_x, top + content_y),
                  (left + content_x + content_w, top + content_y + content_h),
                  (0, 165, 255), 1)
    cv2.putText(canvas, f"ROI {width}x{height} @({left},{top})", (left, max(18, top - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
    if screen_point is not None:
        client_x = screen_point[0] - geometry.window_rect[0]
        client_y = screen_point[1] - geometry.window_rect[1]
        cv2.drawMarker(canvas, (int(round(client_x)), int(round(client_y))),
                       (0, 0, 255), cv2.MARKER_CROSS, 26, 2)
        cv2.putText(canvas, f"screen ({screen_point[0]:.1f},{screen_point[1]:.1f})",
                    (int(client_x) + 12, int(client_y) - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1, cv2.LINE_AA)
    return canvas


def _demo(image_path: Path, *, point: Optional[Tuple[float, float]], frame_id: int,
          out_dir: Path) -> int:
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        print(f"cannot read {image_path}")
        return 1
    height, width = image.shape[:2]
    frame = build_slot_frame(image, frame_id=frame_id)
    ok, reason = check_caps(frame)
    print(f"source            : {image_path.name} {width}x{height}")
    print(f"roi box (l,t,w,h) : {frame.geometry.box}  "
          f"(lie_roi_box says {lie_roi_box(width, height)})")
    print(f"geometry          : {frame.geometry.describe()}")
    print(f"{frame.describe()}")
    print(f"caps              : {'OK' if ok else 'OVER'} {reason}")
    decoded = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    print(f"decoded back      : {decoded.shape[1]}x{decoded.shape[0]} "
          f"mean={decoded.mean(axis=(0, 1)).round(1)}")
    sent = frame.save(out_dir)
    print(f"sent frame saved  : {sent}  (named by the protocol frame_id, "
          f"{frame.frame_id}.jpg)")
    number = point if point is not None else (SLOT_W / 2.0, SLOT_H / 2.0)
    crop_xy = frame.geometry.slot_to_crop_point(*number)
    client_xy = frame.geometry.slot_to_client_point(*number)
    screen_xy = frame.geometry.slot_to_screen_point(*number)
    print(f"server point      : slot {number} -> crop ({crop_xy[0]:.1f},{crop_xy[1]:.1f}) "
          f"-> client ({client_xy[0]:.1f},{client_xy[1]:.1f}) "
          f"-> screen ({screen_xy[0]:.1f},{screen_xy[1]:.1f})")
    back = frame.geometry.screen_to_slot_point(*screen_xy)
    print(f"round trip        : screen {screen_xy} -> slot ({back[0]:.2f},{back[1]:.2f})")
    out_dir.mkdir(parents=True, exist_ok=True)
    preview = cv2.resize(decoded, (SLOT_W * 3, SLOT_H * 3), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(out_dir / "slot_preview.jpg"), preview,
                [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    cv2.imwrite(str(out_dir / "slot_source_annotated.jpg"),
                _annotate_source(image, frame.geometry, screen_xy),
                [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    print(f"previews          : {out_dir / 'slot_preview.jpg'} "
          f"and {out_dir / 'slot_source_annotated.jpg'}")
    return 0


def _frame_files(folder: Path) -> list[Path]:
    """Every JPG frame of a folder, in name order (the project is JPG only)."""

    if not folder.is_dir():
        return []
    return sorted(item for item in folder.iterdir()
                  if item.is_file() and item.suffix.lower() in (".jpg", ".jpeg"))


def _batch(folder: Path, out_dir: Path, *, quality: int, frame_standard: int,
           limit: int = 0) -> int:
    """A folder of frames -> the payloads that would be sent to the server.

    Writes into ``out_dir``:

    * ``<frame_id>.jpg``  - the exact picture sent (byte-identical to the base64 below),
    * ``<frame_id>.json`` - the 2.7.0 §5.1 message for that frame,
    * ``payloads.jsonl``  - every message, one per line, in frame_id order,
    * ``report.json``     - per-frame sizes, caps, geometry and the server's answer space.

    ``frame_id`` follows the protocol: it starts at 1 and increases by one, so the numbering
    here matches what a real round would send (a round ends at the last frame of the folder).
    """

    frames = _frame_files(folder)
    if limit > 0:
        frames = frames[:limit]
    if not frames:
        print(f"no JPG frames in {folder}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    interval = 1.0 / max(1, int(frame_standard))
    report: list[dict] = []
    print(f"source : {folder}  ({len(frames)} jpg frames)")
    print(f"output : {out_dir}")
    print(f"quality: {quality}   frame_standard: {frame_standard} (interval {interval:.4f}s)")
    print()
    print(f"  {'frame_id':>8} {'jpg bytes':>10} {'b64 chars':>10} {'%cap':>6} "
          f"{'encode':>8}  caps")
    with (out_dir / "payloads.jsonl").open("w", encoding="utf-8") as stream:
        for index, path in enumerate(frames, start=1):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                print(f"  {index:>8}  unreadable: {path.name}")
                continue
            frame = build_slot_frame(image, frame_id=index, interval_sec=interval,
                                     quality=quality)
            sent = frame.save(out_dir)
            payload = frame.payload()
            (out_dir / f"{index}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stream.write(json.dumps({"frame_id": index, "payload": payload},
                                    ensure_ascii=False) + "\n")
            ok, reason = check_caps(frame)
            report.append({
                "frame_id": index,
                "source": path.name,
                "sent_jpg": sent.name if sent else None,
                "jpeg_bytes": frame.byte_size,
                "base64_chars": frame.base64_chars,
                "percent_of_byte_cap": round(frame.byte_size * 100.0 / MAX_FRAME_BYTES, 1),
                "encode_ms": round(frame.encode_ms, 2),
                "caps_ok": ok,
                "caps_reason": reason,
                "geometry": frame.geometry.describe(),
                "same_bytes_as_sent": (sent is not None
                                       and sent.read_bytes() == frame.jpeg),
            })
            print(f"  {index:>8} {frame.byte_size:>10} {frame.base64_chars:>10} "
                  f"{frame.byte_size * 100.0 / MAX_FRAME_BYTES:>5.1f}% "
                  f"{frame.encode_ms:>7.1f}ms  {'OK' if ok else 'OVER ' + reason}")
    if not report:
        print("nothing was written")
        return 1
    summary = {
        "source_folder": str(folder),
        "frames": len(report),
        "quality": int(quality),
        "frame_standard": int(frame_standard),
        "frame_id_rule": "2.7.0 §3: starts at 1 for every round, increases by one",
        "geometry": report[0]["geometry"],
        "max_jpeg_bytes": max(item["jpeg_bytes"] for item in report),
        "max_base64_chars": max(item["base64_chars"] for item in report),
        "max_percent_of_cap": max(item["percent_of_byte_cap"] for item in report),
        "all_caps_ok": all(item["caps_ok"] for item in report),
        "all_bytes_match": all(item["same_bytes_as_sent"] for item in report),
        "per_frame": report,
    }
    (out_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"wrote {out_dir}\\<frame_id>.jpg + <frame_id>.json for {len(report)} frames, "
          f"plus payloads.jsonl and report.json")
    print(f"largest jpeg {summary['max_jpeg_bytes']} B "
          f"({summary['max_percent_of_cap']:.1f}% of the {MAX_FRAME_BYTES} cap), "
          f"largest base64 {summary['max_base64_chars']} chars; "
          f"caps ok: {summary['all_caps_ok']}, "
          f"sent bytes == base64 bytes: {summary['all_bytes_match']}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RoiTrack step 1: screenshot -> frame payload")
    parser.add_argument("--demo", type=Path, help="screenshot (PNG/JPG) to convert")
    parser.add_argument("--frames", type=Path,
                        help="folder of JPG frames -> write the payloads it would send")
    parser.add_argument("--out", type=Path, default=ROOT / "work",
                        help="output folder (--frames mode writes the payload set here)")
    parser.add_argument("--quality", type=int, default=JPEG_QUALITY)
    parser.add_argument("--frame-standard", type=int, default=DEFAULT_FRAME_STANDARD)
    parser.add_argument("--limit", type=int, default=0, help="only the first N frames")
    parser.add_argument("--point", default="", help="server answer to map, e.g. 186,124")
    parser.add_argument("--frame-id", type=int, default=1)
    parser.add_argument("--box", action="store_true", help="print the ROI box per client size")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.box:
        for size in ((1366, 768), (1920, 1080), (1080, 768), (1280, 720)):
            box = lie_roi_box(*size)
            print(f"  client {size[0]}x{size[1]}: box(l,t,w,h)={box} "
                  f"aspect={box[2] / box[3]:.4f} slot_scale=({box[2] / SLOT_W:.4f},"
                  f"{box[3] / SLOT_H:.4f})")
        return 0
    if args.frames is not None:
        return _batch(args.frames, args.out, quality=args.quality,
                      frame_standard=args.frame_standard, limit=args.limit)
    if args.demo is None:
        parser.print_help()
        return 2
    point: Optional[Tuple[float, float]] = None
    if args.point:
        try:
            first, second = args.point.split(",")
            point = (float(first), float(second))
        except ValueError:
            print("--point wants 'x,y'")
            return 2
    return _demo(args.demo, point=point, frame_id=args.frame_id, out_dir=args.out)


if __name__ == "__main__":
    raise SystemExit(main())
