"""Geometry of the lie window ("second window") - the measured game-UI preset.

The local Cutie pass that used to own these numbers is gone (lie detection is done by the remote
RoiTrack service now), but the geometry is still needed: the API pass must crop exactly the lie
popup, and everything about it was measured on the operator's 1366x768 client captures.

Reference (1366x768): the popup is a centred **767x598** panel with bright chrome, a gold/brown
body and the fading white target inside.  1366x768 and 1920x1080 share the same game-UI preset, so
every number here is the same pixel value on both; below 1366px wide the whole set scales by the
width ratio (e.g. the 1075x768 client).

Measured on the operator's recordings (kept with the numbers that depend on them):

* the popup's last ~70px is a flat blue-grey bar with no part of the target in it
  (:func:`trim_lie_window`);
* the popup is drawn with a bright frame, a header strip and a bottom bar; feeding the popup's own
  rectangle puts that frame into the feed, so a target reaching the edge merges with it and the aim
  sticks at the border.  The feed therefore cuts the frame off per side
  (:data:`LIE_FEED_INSET_LEFT_PX` ... :func:`inset_lie_window`).

Pure geometry: no capture, no model, no I/O.
"""

from __future__ import annotations

from typing import Tuple

# The reference client these measurements were taken on.
HUD_REFERENCE_WIDTH = 1366
LIE_WINDOW_REFERENCE_CLIENT = (1366, 768)
LIE_WINDOW_REFERENCE_SIZE = (767, 598)
# Presets whose UI is rendered at a genuinely different width; empty today (1366 and 1920 share the
# preset, so they must NOT be listed here).
LIE_PRESET_UI_WIDTHS: dict = {}
LIE_PRESET_MATCH_TOLERANCE = 8

# How much of the popup's own rectangle is cut off before the feed (see the module docstring).
LIE_FEED_INSET_LEFT_PX = 15
LIE_FEED_INSET_RIGHT_PX = 15
LIE_FEED_INSET_TOP_PX = 35
LIE_FEED_INSET_BOTTOM_PX = 70
# The popup's bottom bar, as a ratio of the popup height (12% -> 72px at the reference).
LIE_WINDOW_BOTTOM_TRIM_RATIO = 0.12
# Margin kept around a detected popup so a target at its edge stays inside the feed.
LIE_FEED_MARGIN_RATIO = 0.20
LIE_FEED_MARGIN_MIN_PX = 150


def hud_scale(width: int) -> float:
    """HUD scale factor for a client ``width`` (1.0 at/above the reference)."""

    return min(1.0, max(0.0, float(width) / float(HUD_REFERENCE_WIDTH)))


def lie_ui_scale(client_width: int) -> float:
    """Popup/HUD scale for a client width (the 1366x768 preset is 1.0).

    Presets listed in :data:`LIE_PRESET_UI_WIDTHS` render the UI at their own (different) UI width;
    everything else follows the HUD curve (shrinking below the 1366px reference, fixed at/above it).
    """

    try:
        width = int(client_width)
    except (TypeError, ValueError):
        return 1.0
    for preset_width, preset_ui_width in LIE_PRESET_UI_WIDTHS.items():
        if abs(width - preset_width) <= LIE_PRESET_MATCH_TOLERANCE:
            return max(0.05, preset_ui_width / float(HUD_REFERENCE_WIDTH))
    return hud_scale(width)


def lie_window_size_bounds(width: int, height: int) -> Tuple[int, int, int, int]:
    """Accepted popup SIZE in pixels: (min_w, max_w, min_h, max_h).

    Everything is derived from the 1366x768 preset's measured popup (767x598) and scaled by the
    width-based preset ratio, so the numbers are identical on any client that shares that preset -
    1366x768 and 1920x1080 give exactly the same bounds - and shrink in the same fixed ratio as the
    UI on a smaller client such as 1075x768.  No bounds depend on the current frame size.
    """

    scale = lie_ui_scale(width)
    popup_width = LIE_WINDOW_REFERENCE_SIZE[0] * scale
    popup_height = LIE_WINDOW_REFERENCE_SIZE[1] * scale
    # No clamping to the client: a connected component cannot be larger than the frame anyway, and
    # staying unclamped keeps the bounds identical across every client that shares the preset.
    minimum_width = max(16, int(round(popup_width * 0.70)))
    maximum_width = max(minimum_width, int(round(popup_width * 1.35)))
    minimum_height = max(16, int(round(popup_height * 0.70)))
    maximum_height = max(minimum_height, int(round(popup_height * 1.35)))
    return minimum_width, maximum_width, minimum_height, maximum_height


def lie_window_box(width: int, height: int) -> Tuple[int, int, int, int]:
    """Centred box of the lie popup in client pixels -> (left, top, width, height).

    The popup is a game-UI preset: 1366x768 and 1920x1080 share it, so the measured 767x598 holds
    on both (:func:`lie_ui_scale` only changes it for a genuinely different preset or a smaller
    client).  At the 1366x768 reference this returns (299, 85, 767, 598).
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


def trim_lie_window(box: Tuple[int, int, int, int], *,
                    ratio: float = LIE_WINDOW_BOTTOM_TRIM_RATIO) -> Tuple[int, int, int, int]:
    """Drop the popup's own bottom bar from a lie window box.

    Measured on a live capture of the second window: the popup is 789x603 px there and its last
    ~70 px is a flat blue-grey bar, colour (192,205,220) / (105,126,149), with no part of the fading
    target in it - the target sits in the gold body ~230 px lower down.  Feeding that bar only adds
    a large uniform structure to the crop.
    """

    left, top, width, height = (int(value) for value in box)
    trim = int(round(height * max(0.0, min(0.5, float(ratio)))))
    return (left, top, max(1, width), max(1, height - trim))


def inset_lie_window(box: Tuple[int, int, int, int], *,
                     left_inset: int = LIE_FEED_INSET_LEFT_PX,
                     right_inset: int = LIE_FEED_INSET_RIGHT_PX,
                     top_inset: int = LIE_FEED_INSET_TOP_PX,
                     bottom_inset: int = LIE_FEED_INSET_BOTTOM_PX) -> Tuple[int, int, int, int]:
    """The feed: the CONTENT of the window, its bright frame excluded.

    The window is drawn with a bright border/frame and carries a header strip at the top and a bar
    at the bottom, while the target moves inside it.  Feeding the window's rectangle puts all of
    that into the feed, so a target that reaches the window's edge merges with the frame and the
    aim sticks to the edge.
    """

    left, top, width, height = (int(value) for value in box)
    left_inset = max(0, int(left_inset))
    right_inset = max(0, int(right_inset))
    top_inset = max(0, int(top_inset))
    bottom_inset = max(0, int(bottom_inset))
    width = max(8, width - left_inset - right_inset)
    height = max(8, height - top_inset - bottom_inset)
    return (left + left_inset, top + top_inset, width, height)


def lie_feed_insets(client_width: int) -> Tuple[int, int, int, int]:
    """The per-side feed insets for a client of this width (scaled like every HUD measurement)."""

    scale = lie_ui_scale(client_width)
    return (
        max(2, int(round(LIE_FEED_INSET_LEFT_PX * scale))),
        max(2, int(round(LIE_FEED_INSET_RIGHT_PX * scale))),
        max(2, int(round(LIE_FEED_INSET_TOP_PX * scale))),
        max(2, int(round(LIE_FEED_INSET_BOTTOM_PX * scale))),
    )


def inset_lie_window_for(box: Tuple[int, int, int, int],
                         client_width: int) -> Tuple[int, int, int, int]:
    """The feed for a window detected on a client of this width."""

    left, right, top, bottom = lie_feed_insets(client_width)
    return inset_lie_window(box, left_inset=left, right_inset=right,
                            top_inset=top, bottom_inset=bottom)


def lie_feed_box(width: int, height: int) -> Tuple[int, int, int, int]:
    """The rectangle handed to the tracker, from the client size alone.

    The second window is a game-UI preset that is always centred, so this is a CONSTANT for a given
    client size: the centred popup minus its own frame and title bar.  Nothing is detected per
    frame, which is what keeps the feed - and the rectangle drawn on screen - perfectly steady:
    measured raw detections of the same window differ by up to 38 px per side between frames, and
    adopting them moved the feed, and the target's coordinate origin, every time.
    """

    return inset_lie_window_for(lie_window_box(width, height), width)


def grow_lie_window(box: Tuple[int, int, int, int], width: int, height: int, *,
                    ratio: float = LIE_FEED_MARGIN_RATIO,
                    minimum: int = LIE_FEED_MARGIN_MIN_PX) -> Tuple[int, int, int, int]:
    """A box plus a margin, clamped to the client - keeps a target at the edge inside the feed."""

    left, top, box_width, box_height = (int(value) for value in box)
    margin_x = max(int(minimum), int(round(box_width * max(0.0, float(ratio)))))
    margin_y = max(int(minimum), int(round(box_height * max(0.0, float(ratio)))))
    right = min(int(width), left + box_width + margin_x)
    bottom = min(int(height), top + box_height + margin_y)
    left = max(0, left - margin_x)
    top = max(0, top - margin_y)
    return (left, top, max(1, right - left), max(1, bottom - top))


__all__ = [
    "HUD_REFERENCE_WIDTH", "LIE_FEED_INSET_BOTTOM_PX", "LIE_FEED_INSET_LEFT_PX",
    "LIE_FEED_INSET_RIGHT_PX", "LIE_FEED_INSET_TOP_PX", "LIE_FEED_MARGIN_MIN_PX",
    "LIE_FEED_MARGIN_RATIO", "LIE_PRESET_MATCH_TOLERANCE", "LIE_PRESET_UI_WIDTHS",
    "LIE_WINDOW_BOTTOM_TRIM_RATIO", "LIE_WINDOW_REFERENCE_CLIENT", "LIE_WINDOW_REFERENCE_SIZE",
    "grow_lie_window", "hud_scale", "inset_lie_window", "inset_lie_window_for", "lie_feed_box",
    "lie_feed_insets", "lie_ui_scale", "lie_window_box", "lie_window_size_bounds",
    "trim_lie_window",
]
