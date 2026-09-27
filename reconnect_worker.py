"""自动重连 (automatic reconnect): 掉线 -> login page -> world -> channel -> log in.

The assistant already knows when the game dropped the character: the 掉线 event of the
character detector (its yellow marker disappears for several frames).  That event alone is
not proof, so this worker requires a SECOND sign before it touches anything: the game window
must show the login page's own BASE COLOUR (screenshots/login_page_target.jpg is a crop of
the page's cream background, so it defines a colour to look for - not a shape to correlate).
Only then does it act:

  1. press Enter, wait 3s (the login page closes),
  2. first select window: walk down to the chosen world and confirm with Enter,
  3. that opens the second select window on channel 1: move to the target channel with
     right/down, Enter, wait 2s, Enter.

The whole sequence is **keyboard only**: the game opens each list on its first entry, so nothing
about the select windows has to be measured, calibrated or clicked - which is why there is no
window capture or calibration button in the panel.  (The 1366x768 game window is fixed art: the
lists are the same at any client size because the keys, not pixels, do the selecting.)

Every step is reported to the UI through a result queue, so the operator can see what the
worker did and where it stopped.  Nothing here uses Cutie or any tracking model.
"""

from __future__ import annotations

import logging
import os
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
# The shipped reference/asset folder (the same one the login colour crop travels in).
ASSETS_DIR = ROOT / "recording-assets"

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
LOGIN_WAIT_SECONDS = 1.2
SELECT_WINDOW_WAIT_SECONDS = 0.8
# Time a single key hold and the pause between two channel moves.
KEY_HOLD_SECONDS = 0.05
KEY_DELAY_SECONDS = 0.15
# The login page's Enter needs a longer press than the relay keys, and it is retried: measured in
# the field (v0391, 22:35) the page stayed on screen although the key was reported delivered, and
# the rest of the sequence then typed into the login page (world/channel keys did nothing).
LOGIN_ENTER_HOLD_SECONDS = 0.15
LOGIN_ENTER_ATTEMPTS = 3
# How much the window must change (mean absolute grey-level difference over a coarse thumbnail)
# before the login page counts as having reacted to Enter.  A UI switch is far above this; a static
# page with a blinking caret is far below (measured: the assistant's own frames differ by <0.5).
SCREEN_CHANGE_MIN = 2.0
SCREEN_CHANGE_THUMBNAIL = (64, 36)
# The assistant's own window can take the foreground back mid-sequence (measured at 22:35:23: the
# game lost focus to `MapleAssistant (v0391)`), so a key is retried after bringing the game back
# instead of failing the whole run.
KEY_REFOCUS_ATTEMPTS = 2
KEY_REFOCUS_WAIT_SECONDS = 0.25
# The game only starts accepting keys after its own window has been CLICKED: measured in the field
# (v0391/v0392) the window was foreground and the keys were reported delivered, yet the login page
# ignored Enter.  The operator's order is therefore: focus the window, click a harmless spot inside
# the client, then press Enter.  (50, 50) is his choice - inside the client, far from every button
# and list entry.
ACTIVATE_CLICK_CLIENT = (50, 50)
ACTIVATE_CLICK_WAIT_SECONDS = 0.8
# 掉线提示窗口 (operator's rule, 2026-09-17): the moment the connection drops the game shows a prompt
# over the client whose default button is 确定, and the login board behind it cannot be used until it
# is closed.
#
# ``screenshots/offline_prompt.jpg`` shows the prompt to close. Before any
# login interaction, detect its peach panel from the supplied 40x40
# ``offline_prompt_square.png`` crop. A small colour tolerance handles normal
# capture/Windows-scaling variation in the paper texture.
#
# The ``确定`` centre is (middle, 330) on the 1080x768 preset. The prompt art
# scales with client width, so the 1366x768 point is (683, 417).
#
# The button is CLICKED first and Enter is only the fallback: "this is the close prompt window button,
# you either click it or you press enter (press enter seem to fail)".  Measured earlier on the same
# page: while the prompt is up the login board is COVERED - the 连接 patch scores 0.031 there (0.994 on
# the real login page), and both the board point (647, 326) and the 连接 point (721, 401) land on the
# prompt - so the workflow must not start until the prompt is really gone.  The page still passes the
# login-page COLOUR gate (9.2 % cream), so the page classifier is what tells them apart.
OFFLINE_PROMPT_REFERENCE_CLIENT: tuple[int, int] = (1080, 768)
OFFLINE_PROMPT_CLOSE_REFERENCE_POINT: tuple[int, int] = (540, 330)
OFFLINE_PROMPT_SQUARE_NAME = "offline_prompt_square.png"
OFFLINE_PROMPT_COLOUR_TOLERANCE = 28
OFFLINE_PROMPT_MIN_COVERAGE = 0.90
OFFLINE_PROMPT_CLICK = True
OFFLINE_PROMPT_ENTER = True
OFFLINE_PROMPT_WAIT_SECONDS = 0.25
OFFLINE_PROMPT_ATTEMPTS = 3
# A reconnect may reach the game before the minimap and its yellow character marker are painted.
# Restart patrol as soon as a short consecutive run of real marker samples proves the game is ready,
# instead of blindly sleeping for a fixed amount of time.
POST_LOGIN_MARKER_CONFIRM_SAMPLES = 3
POST_LOGIN_MARKER_POLL_SECONDS = 0.20  # shared capture cadence: 5 fps
POST_LOGIN_MARKER_TIMEOUT_SECONDS = 20.0
# The login board and its buttons, measured by the operator on his own login page.  The 1366x768
# numbers are the preset space; he re-measured them on his second device (a 1080x768 client,
# 2026-09-17, work/anchors_1080.json) and the numbers below are those measurements mapped back into
# this space (the board keeps its pixel size and is centred, see ``login_client_point``):
#
#   * the board spans client x 679..1049, y 190..582 (his hand-drawn area 536,190 370x392),
#   * the **连接** button's centre is (864, 401) (his 1080 click (721, 401)),
#   * the panel point that gives the game a click first is (790, 326) (his 1080 click (647, 326)),
#   * the **结束游戏 (exit game)** button is NEVER clicked - both measurements are kept as guards.
#
# v0394 clicked a guessed fraction of a wrongly-measured "board" and hit 结束游戏 in the field, so
# from now on only ``LOGIN_CONNECT_CLICK_CLIENT`` (or a located OpenCV button) is clicked, and only
# when that point lies inside ``LOGIN_BOARD_CLIENT_BOX`` and outside every exit-button box.
LOGIN_BOARD_CLIENT_BOX = (679, 190, 370, 392)
# The operator's own clicks - the 1080 re-measurement of 2026-09-17 (his board point (647, 326) and
# the 连接 label's centre (721, 401)), which also sit where the shipped 1366 patch of the real button
# puts its centre (860, 400): both are on the button, and the fresh one is 4 px closer to the middle.
LOGIN_BOARD_CLICK_CLIENT: tuple[int, int] | None = (790, 326)
LOGIN_CONNECT_CLICK_CLIENT: tuple[int, int] | None = (864, 401)
# Recorded so no code path can ever click it.  Two boxes: the first was measured on his 1366 login
# page (screenshots/login_page.jpg), the second on his 1080 login page, where that button is NOT at
# the mapped position - the guard has to know both, because a click on it ends the game.
LOGIN_EXIT_BUTTON_CLIENT_BOX = (1120, 582, 145, 55)
LOGIN_EXIT_BUTTON_CLIENT_BOXES = (
    LOGIN_EXIT_BUTTON_CLIENT_BOX,
    (1040, 467, 95, 29),                 # his 1080 pick (897, 467, 95x29), mapped back
)
# The WORLD list: measured from the operator's own clicks on screenshots/channel_select_first.jpg.
# The five worlds are in ONE HORIZONTAL LINE (his clicks: (498,163) and (602,163)), so the list is
# reached with LEFT/RIGHT, not up/down - which is exactly why the old "press down (row-1) times"
# left the default 蓝蜗牛 selected and Enter confirmed it.  World w is clicked directly:
# Confirmed again on 2026-09-17 for the 1080 client: 蘑菇仔 is to the RIGHT of 蓝蜗牛 (he answered
# "右边（向右 82px）" = 104 x 0.7906), and his 1080 pick of 蓝蜗牛 (392, 209) is within 2 px of where
# this preset maps to.  A second pick he made 22 px lower was NOT a world - the step stays horizontal.
WORLD_ROW_CLIENT = (498, 163)            # centre of 蓝蜗牛 (world 1)
WORLD_ROW_STEP_CLIENT = (104, 0)         # world 2 sits 104 px to its right
# The same dilution trap as the channel list: the highlight that moves when a world is clicked is
# ~104x30 px inside a 731x507 window, which a 64x36 whole-frame thumbnail averages away.  The world
# row itself is therefore measured too, so a click that DID work never falls back to the keyboard.
WORLD_LIST_CLIENT_BOX = (430, 110, 590, 110)     # client x, y, width, height of the one-row list
WORLD_LIST_CHANGE_MIN = 1.0
# The CHANNEL grid: the operator's channel 1 click is (556, 387) and channel 2 is (650, 385) - 94 px
# to the right - and the window shows **4 channels per row** (his measurement; 20 channels = 5 rows).
# Only channel 1 is ever clicked, then the keyboard walks to the target, because the window does not
# show every channel at once (his rule: "click channel 1 and use keyboard to move channel").
CHANNEL_FIRST_CLIENT = (555, 388)          # channel 1 (his click)
CHANNEL_STEP_CLIENT = 94                   # channel 2 is 94 px to the right (his click)
CHANNEL_ROW_STEP_CLIENT = 30.5             # fitted from his clicks on rows 1..5
CHANNEL_VISIBLE_ROWS = 5                   # rows on screen at scroll top 0
CHANNELS_PER_ROW = 4
# The list scrolls: one wheel notch shifts it by one row (4 channels) - the operator's measurement.
# The walk therefore moves down with the keyboard and scrolls one row whenever the selection cannot
# move any further, so a channel that is not on screen is still reachable.
CHANNEL_SCROLL_NOTCHES_PER_ROW = 1
CHANNEL_SCROLL_WAIT_SECONDS = 0.4
CHANNEL_SCROLL_ATTEMPTS = 2                      # a wheel can be swallowed while focus settles
# The scroll is CLOSED-LOOP (v0402, the operator's channel-51 report): one notch is sent at a time,
# each notch is verified on the list region, and how far the list really moved is MEASURED by
# matching the rows against the frame from before the notch.  So "one notch = one row" is checked
# instead of trusted: a swallowed notch is retried, a notch that jumps two rows is counted as two,
# and the rows actually reached are what the cell position is computed from.
CHANNEL_SCROLL_SLACK_NOTCHES = 4                 # extra notches allowed (swallowed events)
CHANNEL_SCROLL_MATCH_ROWS = 3                    # rows of margin above the matched strip
CHANNEL_SCROLL_MATCH_MIN_SCORE = 0.40            # matchTemplate score below this = not measurable
# The cell is CLICKED and then DOUBLE-CLICKED (the operator's rule; the double click is what enters
# the channel - "press enter will go into channel1", so Enter is NOT a substitute).
#
# His field report of 2026-09-17 (channel 40, twice): the click selected the cell but "the double click
# seemed failed", while HIS OWN mouse double click always works.  Two corrections came from the field:
# the clicks must be injected with SendInput (the game ignores the legacy mouse_event - fixed in
# v1.0.23) and the pair must be QUICK, not slow ("i think maybe the double click is to slow make it
# quicker, and double click twice").  So: a short press, a short gap, and the pair sent twice.
DOUBLE_CLICK_HOLD_SECONDS = 0.04         # a quick press down/up
DOUBLE_CLICK_GAP_SECONDS = 0.07          # quick: the two clicks of one pair
DOUBLE_CLICK_SLOW_GAP_SECONDS = 0.07     # the second pair uses the same quick timing
DOUBLE_CLICK_GAPS = (DOUBLE_CLICK_GAP_SECONDS, DOUBLE_CLICK_SLOW_GAP_SECONDS)
# The shipped click timing for every other click (place the cursor, wait, press, hold, release).
CLICK_SETTLE_SECONDS = 0.03
CLICK_HOLD_SECONDS = 0.03
# Every click is injected with SendInput - the same path as this project's keys, trade clicks and
# wheel.  ``mouse_event`` is only the fallback: this game is known to ignore it, and that is exactly
# what made the channel cell impossible to enter (2026-09-17: "i can see that the mouse is double
# clicking but it just cannot enter that channel, however my manual operation is good" - the hover was
# delivered, the button press was not).
MOUSE_MOVE_ABSOLUTE = 0x0001 | 0x8000 | 0x4000   # MOVE | ABSOLUTE | VIRTUALDESK
MOUSE_LEFT_DOWN = 0x0002
MOUSE_LEFT_UP = 0x0004
# The operator's v0400 report: "on scroll the channel row the row is changed but you detected it as
# not scrolled".  The wheel was fine - the MEASUREMENT was wrong: the change was a 64x36 grey
# thumbnail of the WHOLE 1366x768 frame, so a one-row (30 px) shift of the list moved that thumbnail
# by about one pixel and came out below SCREEN_CHANGE_MIN.  Every channel-list decision therefore
# measures the list region ITSELF, and waits several frames for the game to repaint: a scroll is
# only "not done" when the list is still unchanged ~1.8 s later.
CHANNEL_LIST_CLIENT_BOX = (498, 340, 500, 226)   # client x, y, width, height of the 4-per-row grid
CHANNEL_LIST_CHANGE_MIN = 1.0                    # on that crop - a moved highlight clears this
CHANNEL_LIST_POLL_SECONDS = 0.30                 # between two repaint checks
CHANNEL_LIST_SETTLE_ATTEMPTS = 6                 # ~1.8 s before "the list did not react"
# How many times a click is retried (after re-focusing) before the step gives up.
CLICK_ATTEMPTS = 3
CLICK_RETRY_WAIT_SECONDS = 0.4
# Safety clamp: a click is only sent inside the window area the two select frames differ in, so a
# wrong constant can never click somewhere else in the game.
SELECT_WINDOW_CLIENT_BOX = (292, 59, 731, 507)
# The 连接 button: a filled, coloured rectangle on the board, so it is found by saturation + shape
# rather than by reading text.
CONNECT_BUTTON_SATURATION_MIN = 60
CONNECT_BUTTON_VALUE_MIN = 60
CONNECT_BUTTON_MIN_AREA_RATIO = 0.002      # of the board area
CONNECT_BUTTON_WIDTH_RANGE = (0.06, 0.60)  # of the board width
CONNECT_BUTTON_HEIGHT_RANGE = (0.02, 0.16)  # of the board height
CONNECT_BUTTON_ASPECT_RANGE = (1.2, 14.0)
CONNECT_BUTTON_REFERENCE_NAME = "login_connect_target.jpg"
# Template matching threshold for the 连接 button crop (a shipped crop must match the real art).
CONNECT_TEMPLATE_MIN_SCORE = 0.62
# The final channel handoff is deliberately three quiet rounds: wait for the client
# to accept the selected channel, then double-tap Enter.  A rapid continuous
# Enter loop is often discarded by the client at this boundary.
CHANNEL_CONFIRM_ROUND_WAIT_SECONDS = 2.0
CHANNEL_CONFIRM_DOUBLE_PRESS_GAP_SECONDS = 0.12
CHANNEL_CONFIRM_ROUNDS = 3
# During the final channel handoff, a real minimap marker is stronger evidence
# than a completed Enter routine.  Poll at the shared capture cadence so an
# already-loaded game immediately cancels the remaining confirmation keys.
CHANNEL_CONFIRM_MARKER_POLL_SECONDS = 0.20
# When no usable game window exists, retry a *fresh title search* at this pace.
# The shared WindowKeySender itself first tries its current handle and only then
# re-anchors to a newly created game window.
FOCUS_REANCHOR_INTERVAL_SECONDS = 10.0
# Pause after logging in, before the worker reports the reconnect as finished.
LOGIN_SETTLE_SECONDS = 1.0


# The reference client size the keyboard sequence is written for.  Nothing about the
# sequence depends on where the window sits or how big it is: the world list is clicked and the
# channel list is walked with the keyboard from the clicked channel 1.
REFERENCE_CLIENT: tuple[int, int] = (1366, 768)

# ---------------------------------------------------------- mapping the presets to the real client
# Every client constant above was measured on a 1366x768 client.  The operator runs the same build on
# a **1080x768** client as well ("reconnect on this device failed", 2026-09-17), and the two families
# of UI elements move differently - both measured on his own 1080 screenshots with the shipped
# detectors, not guessed:
#
#   * the LOGIN page keeps its pixel size and is centred in the client, so the whole board only
#     shifts by half the width difference: ``page_login_connect_target.jpg`` (a 160x40 crop of the
#     real 连接 button) matched screenshots/1080_login_page.png at scale **1.000** exactly
#     143 px = (1366-1080)/2 to the left (score 0.991), and the operator's hand-drawn board area came
#     out at x 536 = 680 - 144.  y does not move (both clients are 768 tall).
#   * the WORLD/CHANNEL windows scale with the client WIDTH about the client's vertical centre:
#     ``page_world_grid1/grid2`` matched screenshots/1080_wolrd_select.png at scale **0.790** with
#     their centre y unchanged (score 0.978/0.976), and every one of the operator's clicks came back
#     out of the inverse mapping within 3 px of its preset (world row 1 -2 px, channel 1 +3 px,
#     channel column pitch 75 vs 94 x 0.7906 = 74.3; the channel row bands measured 24.5 px against
#     30.5 x 0.7906 = 24.1).
#
# Nothing above changes: the presets stay the 1366 measurements and are mapped where they are used.
# ``_client_size()`` supplies the size to map into (the captured frame, which is the client).
ANCHOR_SPACE_LOGIN = "login"        # centred, unscaled (the login board and its buttons)
ANCHOR_SPACE_WINDOW = "window"      # scaled with the width (the world and channel windows)


def _client_size_tuple(client_size) -> tuple[int, int]:
    """A usable ``(width, height)`` - the reference client when nothing sensible was given."""

    try:
        width, height = int(client_size[0]), int(client_size[1])
    except Exception:
        return REFERENCE_CLIENT
    if width <= 0 or height <= 0:
        return REFERENCE_CLIENT
    return (width, height)


def login_client_point(point, client_size=REFERENCE_CLIENT):
    """A preset login-page client point -> the same element on a ``client_size`` client."""

    if point is None:
        return None
    width, height = _client_size_tuple(client_size)
    offset_x = (width - REFERENCE_CLIENT[0]) / 2.0
    offset_y = (height - REFERENCE_CLIENT[1]) / 2.0
    return (int(round(point[0] + offset_x)), int(round(point[1] + offset_y)))


def login_client_box(box, client_size=REFERENCE_CLIENT):
    """A preset login-page client box -> the same element (same size) on a ``client_size`` client."""

    if box is None:
        return None
    left, top, width, height = (int(value) for value in box)
    left, top = login_client_point((left, top), client_size)
    return (left, top, width, height)


def offline_prompt_close_client_point(client_size=REFERENCE_CLIENT) -> tuple[int, int]:
    """Map the offline-prompt ``确定`` centre by the client-width ratio."""

    width, _height = _client_size_tuple(client_size)
    reference_width, _reference_height = OFFLINE_PROMPT_REFERENCE_CLIENT
    x, y = OFFLINE_PROMPT_CLOSE_REFERENCE_POINT
    scale = width / float(reference_width)
    return (
        int(round(x * scale)),
        int(round(y * scale)),
    )


def window_client_scale(client_size=REFERENCE_CLIENT) -> float:
    """How much the world/channel windows are scaled on a ``client_size`` client."""

    width, _height = _client_size_tuple(client_size)
    return width / float(REFERENCE_CLIENT[0])


def window_client_point(point, client_size=REFERENCE_CLIENT):
    """A preset world/channel-window point -> the same element on a ``client_size`` client."""

    if point is None:
        return None
    scale = window_client_scale(client_size)
    if abs(scale - 1.0) < 1e-6:
        return (int(point[0]), int(point[1]))
    _width, height = _client_size_tuple(client_size)
    centre = height / 2.0
    return (int(round(point[0] * scale)), int(round(centre + scale * (point[1] - centre))))


def window_client_box(box, client_size=REFERENCE_CLIENT):
    """A preset world/channel-window box -> the same element (and size) on this client."""

    if box is None:
        return None
    left, top, width, height = (int(value) for value in box)
    scale = window_client_scale(client_size)
    left, top = window_client_point((left, top), client_size)
    return (left, top, max(1, int(round(width * scale))), max(1, int(round(height * scale))))


def window_client_delta(step, client_size=REFERENCE_CLIENT):
    """A preset world/channel STEP (a distance, not a position) on a ``client_size`` client."""

    if step is None:
        return None
    scale = window_client_scale(client_size)
    return (int(round(step[0] * scale)), int(round(step[1] * scale)))


def window_client_length(value: float, client_size=REFERENCE_CLIENT) -> float:
    """A preset world/channel LENGTH (row pitch, column pitch) on a ``client_size`` client."""

    return float(value) * window_client_scale(client_size)


def client_anchor(space: str, anchor, client_size=REFERENCE_CLIENT):
    """A preset point or box of a named space -> this client (the dispatcher for the two families)."""

    if space == ANCHOR_SPACE_LOGIN:
        return login_client_box(anchor, client_size) if len(anchor) == 4 else \
            login_client_point(anchor, client_size)
    return window_client_box(anchor, client_size) if len(anchor) == 4 else \
        window_client_point(anchor, client_size)

# ---------------------------------------------------------------- the page verifier
# The game shows one of three pages during the sequence, and each has a signature the operator
# named himself:
#
#   * PAGE_LOGIN   - the deep brown login board (it holds 连接 and 结束游戏),
#   * PAGE_WORLD   - the world list (蓝蜗牛 蘑菇仔 ...),
#   * PAGE_CHANNEL - the channel list (the grid area, which is empty on the world page).
#
# The world row is identical in both select frames (measured), so the CHANNEL GRID is what tells
# them apart.  Every step now ends by asking which page is on screen and is retried when the answer
# is not the expected one.
PAGE_LOGIN = "login"
PAGE_WORLD = "world"
PAGE_CHANNEL = "channel"
PAGE_LABELS = {
    PAGE_LOGIN: "登录页（连接 / 结束游戏）",
    PAGE_WORLD: "世界选择页（蓝蜗牛…）",
    PAGE_CHANNEL: "频道选择页",
    None: "游戏内",
}
PAGE_MIN_SCORE = 0.60
# Window names that ARE the game's login UI: measured on the operator's machine, the login board
# (with 连接 and 结束游戏) is drawn in a separate process window called `igwUserLoginDialog`.  It is
# accepted by name because refusing it blocks every login click (field log 19:40 and 19:46).
PAGE_LOGIN_WINDOW_HINTS = ("igwUserLoginDialog",)
# A page is identified by SEVERAL small, glyph-dense patches and its score is the MINIMUM of them, so
# every patch has to match.  This replaces the single 340x512 board crop of v0405, which was
# dominated by background and therefore ALSO scored 1.000 on the world page - a coin flip that could
# report the world page while the login page was still on screen (the operator's "login not success
# but going into the second stage").
#
#   patch                        login    world  channel   ingame      (measured, work/pick_page_signatures.py)
#   login 连接 text (780,380)      1.000   -0.010   -0.003   0.067
#   login 结束游戏 (1060,565)      1.000   -0.099   -0.099   0.086
#   world grid1 (500,368)          0.075    1.000    0.075   0.055
#   world grid2 (500,400)          0.039    1.000    0.122   0.044
#   channel grid1 (500,368)       -0.022    0.075    1.000   0.058
#   channel grid2 (500,430)        0.015    0.085    1.000  -0.002
PAGE_LOGIN_CONNECT_BOX = (780, 380, 160, 40)     # the 连接 label
PAGE_LOGIN_EXIT_BOX = (1060, 565, 190, 45)       # the 结束游戏 label - NOT a signature any more
PAGE_GRID_BOX = (500, 368, 420, 40)              # the channel grid, first row
PAGE_GRID_LOWER_BOX = (500, 430, 420, 80)        # the channel grid, lower rows
# The operator's 1080 channel-window pick (613, 333, 107x27), mapped back to the 1366 preset space.
# It replaces the two 1366 channel crops (500,368) and (500,430) as the channel signature: on his
# 1080 client those two only reached 0.622/0.641 - the page would have been "recognised" with 0.02 of
# margin - while his own crop is exact there (1.000) and low on the other pages (world +0.001).
PAGE_CHANNEL_GRID_BOX = (775, 320, 135, 34)
# Each entry carries the anchor space its box is measured in, so the box can be mapped onto the
# client that is really on screen (see the two mapping families above).
#
# The 结束游戏 patch was dropped on 2026-09-17: on the operator's current login page that button is
# NOT where the 1366 crop expects it (the crop scored 0.378 - i.e. nothing - on his own 1080 login
# page), and a page's score is the MINIMUM over its patches, so keeping it refused every 1080 login
# page.  The 连接 patch alone separates the pages (1.000/1.000 login, -0.010 world, -0.003 channel at
# 1366; +0.991 / -0.083 / -0.019 at 1080).
PAGE_REFERENCES = (
    (PAGE_LOGIN, PAGE_LOGIN_CONNECT_BOX, "page_login_connect_target.jpg", ANCHOR_SPACE_LOGIN),
    (PAGE_WORLD, PAGE_GRID_BOX, "page_world_grid1_target.jpg", ANCHOR_SPACE_WINDOW),
    (PAGE_WORLD, (500, 400, 420, 60), "page_world_grid2_target.jpg", ANCHOR_SPACE_WINDOW),
    (PAGE_CHANNEL, PAGE_CHANNEL_GRID_BOX, "page_channel_grid_target.jpg", ANCHOR_SPACE_WINDOW),
)
# How often a step is repeated when the page did not change.
PAGE_STEP_ATTEMPTS = 3
# The game does not react instantly ("the process is stepping too quick, the game isn't react that
# quick"), so after every action the page is POLLED for this long instead of sampled once.
PAGE_POLL_SECONDS = 0.2
PAGE_WAIT_SECONDS = 3.0                  # login page -> world page
PAGE_WAIT_WORLD_SECONDS = 3.0            # world page -> channel page
PAGE_WAIT_SHORT_SECONDS = 1.2            # the board click: it may or may not be enough
# How often the channel Enter is repeated while the channel page stays on screen.
CHANNEL_CONFIRM_ATTEMPTS = 3


def page_scores(frame, references) -> dict:
    """The score of every page signature on a frame.

    A page's score is the MINIMUM over its patches (they all have to match), which is what makes a
    page hard to claim by accident: the v0405 build used one big board crop that was dominated by
    background and scored 1.000 on the world page as well.

    Every box is mapped from the preset (1366x768) space onto the frame's own size first, so the
    patches describe a 1080x768 client just as well (see the mapping families above).  A reference
    given as ``(crop, box)`` without a space is treated as a window-space box, which is what the
    page signatures used to be.
    """

    import cv2

    scores: dict = {}
    if frame is None or not references:
        return scores
    frame_size = (int(frame.shape[1]), int(frame.shape[0]))
    for page, patches in references.items():
        score = None
        for patch in patches:
            crop, box = patch[0], patch[1]
            space = patch[2] if len(patch) > 2 else ANCHOR_SPACE_WINDOW
            mapped = client_anchor(space, box, frame_size)
            window = crop_client_region(frame, mapped)
            if window is None or crop is None:
                score = None
                break
            try:
                if window.shape[:2] != crop.shape[:2]:
                    window = cv2.resize(window, (crop.shape[1], crop.shape[0]),
                                        interpolation=cv2.INTER_AREA)
                found = float(cv2.matchTemplate(window, crop, cv2.TM_CCOEFF_NORMED)[0][0])
            except Exception:
                LOG.debug("auto reconnect: the page signature %s could not be compared", page,
                          exc_info=True)
                score = None
                break
            score = found if score is None else min(score, found)
        if score is not None:
            scores[page] = score
    return scores


def detect_page(frame, references) -> tuple[Optional[str], float]:
    """Which page the game is showing -> ``(page, score)``; page is None when it is none of them."""

    scores = page_scores(frame, references)
    if not scores:
        return (None, 0.0)
    best_page = max(scores, key=lambda page: scores[page])
    best_score = scores[best_page]
    if best_score < PAGE_MIN_SCORE:
        return (None, best_score)
    return (best_page, best_score)


def load_page_references() -> dict:
    """The page signatures: ``{page: [(crop, box, space), ...]}``, missing files simply skipped.

    A page with NO patch at all is dropped: it cannot be identified, and pretending otherwise would
    block the steps.  A page keeps the patches it has, so a partially installed set still works.
    """

    import cv2

    loaded: dict = {}
    for page, box, name, space in PAGE_REFERENCES:
        for folder in (SCREENSHOTS_DIR, ASSETS_DIR):
            path = folder / name
            if not path.is_file():
                continue
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None or not image.size:
                continue
            loaded.setdefault(page, []).append((image, tuple(int(value) for value in box), space))
            LOG.info("auto reconnect: page signature %s loaded from %s (%dx%d, box %s, %s space)",
                     page, path, image.shape[1], image.shape[0], box, space)
            break
    return {page: patches for page, patches in loaded.items() if patches}


def load_offline_prompt_square():
    """Load the supplied colour patch used to identify the offline prompt."""

    import cv2

    for folder in (SCREENSHOTS_DIR, ASSETS_DIR):
        path = folder / OFFLINE_PROMPT_SQUARE_NAME
        if not path.is_file():
            continue
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is not None and image.size:
            LOG.info(
                "auto reconnect: offline prompt square loaded from %s (%dx%d)",
                path,
                image.shape[1],
                image.shape[0],
            )
            return image
    LOG.warning(
        "auto reconnect: offline prompt square is missing; refusing prompt clicks"
    )
    return None


def find_offline_prompt_square(frame, square):
    """Return ``(x, y, width, height, coverage)`` for the prompt panel, if present.

    The supplied square is intentionally a low-detail paper patch, so ordinary
    correlation is not reliable. Instead every pixel is compared with the
    reference colour within :data:`OFFLINE_PROMPT_COLOUR_TOLERANCE`, and a
    scaled square must meet the required coverage. The scaling is tied to
    client width, as is the prompt-button coordinate.
    """

    import cv2
    import numpy as np

    if (frame is None or square is None or not getattr(frame, "size", 0)
            or not getattr(square, "size", 0)):
        return None
    try:
        frame_h, frame_w = frame.shape[:2]
        scale = frame_w / float(OFFLINE_PROMPT_REFERENCE_CLIENT[0])
        patch_w = max(8, int(round(square.shape[1] * scale)))
        patch_h = max(8, int(round(square.shape[0] * scale)))
        if patch_w >= frame_w or patch_h >= frame_h:
            return None
        patch = cv2.resize(square, (patch_w, patch_h), interpolation=cv2.INTER_AREA)
        colour = np.median(patch.reshape(-1, 3), axis=0).astype(np.int16)
        delta = np.abs(frame.astype(np.int16) - colour.reshape(1, 1, 3))
        matches = np.all(delta <= OFFLINE_PROMPT_COLOUR_TOLERANCE, axis=2).astype(np.float32)
        coverage = cv2.boxFilter(
            matches,
            ddepth=-1,
            ksize=(patch_w, patch_h),
            normalize=True,
            borderType=cv2.BORDER_CONSTANT,
        )
        _min, best, _min_loc, best_loc = cv2.minMaxLoc(coverage)
        if float(best) < OFFLINE_PROMPT_MIN_COVERAGE:
            return None
        center_x, center_y = (int(best_loc[0]), int(best_loc[1]))
        left = max(0, min(frame_w - patch_w, center_x - patch_w // 2))
        top = max(0, min(frame_h - patch_h, center_y - patch_h // 2))
        return (left, top, patch_w, patch_h, float(best))
    except Exception:
        LOG.debug("auto reconnect: offline prompt square matching failed", exc_info=True)
        return None


def window_rect(hwnd: int):
    """A window's outer rectangle -> (left, top, right, bottom), or None."""

    try:
        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
        return (int(left), int(top), int(right), int(bottom))
    except Exception:
        return None


def rect_inside(inner, outer, tolerance: int = 6) -> bool:
    """Whether ``inner`` lies inside ``outer`` (with a few pixels of tolerance)."""

    if inner is None or outer is None:
        return False
    left, top, right, bottom = inner
    o_left, o_top, o_right, o_bottom = outer
    return (o_left - tolerance <= left and top >= o_top - tolerance
            and right <= o_right + tolerance and bottom <= o_bottom + tolerance)


def rect_area(rect) -> int:
    if rect is None:
        return 0
    return max(0, int(rect[2]) - int(rect[0])) * max(0, int(rect[3]) - int(rect[1]))


def windows_over_point(x: int, y: int) -> list:
    """Every visible top-level window whose rectangle covers a screen point.

    ``WindowFromPoint`` only reports the single topmost window; when a click is refused it is useful to
    see the whole stack over that point (title, hwnd, process id, topmost/click-through), because a
    third-party overlay is otherwise invisible.
    """

    try:
        import win32con
        import win32gui

        found = []

        def visit(hwnd, _param):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if not (left <= x <= right and top <= y <= bottom):
                return True
            try:
                flags = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            except Exception:
                flags = 0
            found.append({
                "hwnd": int(hwnd),
                "title": win32gui.GetWindowText(hwnd) or win32gui.GetClassName(hwnd),
                "pid": window_process_id(hwnd),
                "topmost": bool(flags & win32con.WS_EX_TOPMOST),
                "click_through": bool(flags & 0x00000020),
            })
            return True

        win32gui.EnumWindows(visit, None)
        return found
    except Exception:
        LOG.debug("auto reconnect: the windows over a point could not be listed", exc_info=True)
        return []


def process_image_name(pid: int) -> str:
    """The executable name of a process (``Maplestory_Classic.exe``), or "" when unknown."""

    if not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return str(buffer.value).rsplit("\\", 1)[-1].casefold()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        LOG.debug("auto reconnect: the image name of pid %s could not be read", pid, exc_info=True)
    return ""


def find_game_windows(title: str) -> list:
    """Every visible top-level window whose title matches -> dicts with rect/class/client size.

    The same title can exist on more than one window (measured: the game and its login client are both
    `Maplestory_Classic.exe` with the title `冒险岛怀旧服`), and picking a different one for the picture
    than for the clicks puts every click at the wrong screen position.
    """

    if not title:
        return []
    try:
        import win32gui

        wanted = title.casefold()
        found = []

        def visit(hwnd, _param):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                window_title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                return True
            if wanted not in window_title.casefold():
                return True
            try:
                client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(hwnd)
                origin = win32gui.ClientToScreen(hwnd, (int(client_left), int(client_top)))
                found.append({
                    "hwnd": int(hwnd),
                    "title": window_title,
                    "class": win32gui.GetClassName(hwnd),
                    "rect": tuple(int(v) for v in win32gui.GetWindowRect(hwnd)),
                    "client_origin": (int(origin[0]), int(origin[1])),
                    "client_size": (int(client_right - client_left),
                                    int(client_bottom - client_top)),
                    "minimized": bool(win32gui.IsIconic(hwnd)),
                })
            except Exception:
                LOG.debug("auto reconnect: window %s could not be measured", hwnd, exc_info=True)
            return True

        win32gui.EnumWindows(visit, None)
        return found
    except Exception:
        LOG.debug("auto reconnect: the game windows could not be listed", exc_info=True)
        return []


def window_process_id(hwnd: int) -> int:
    """The process id that owns a window (0 when it cannot be read)."""

    if not hwnd:
        return 0
    try:
        import win32process

        return int(win32process.GetWindowThreadProcessId(int(hwnd))[1])
    except Exception:
        LOG.debug("auto reconnect: the process of window %s could not be read", hwnd,
                  exc_info=True)
        return 0


def window_at_point(x: int, y: int) -> tuple[int, str, str]:
    """The top-level window that owns a screen point -> (hwnd, title, class name).

    ``WindowFromPoint`` returns the child window under the mouse; the ROOT ancestor is what a click
    would activate, which is the window the game is competing with.
    """

    import win32gui

    hwnd = win32gui.WindowFromPoint((int(x), int(y)))
    if not hwnd:
        return (0, "", "")
    root = win32gui.GetAncestor(hwnd, 2) or hwnd          # GA_ROOT = 2
    title = win32gui.GetWindowText(root) or win32gui.GetWindowText(hwnd)
    try:
        class_name = win32gui.GetClassName(root)
    except Exception:
        class_name = ""
    return (int(root), str(title), str(class_name))


def cursor_screen_position() -> tuple[int, int]:
    """Where the mouse cursor is, in screen pixels."""

    import ctypes

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = _Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return int(point.x), int(point.y)


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

    def is_login_page(self) -> bool:
        """The gate the worker and the panel both use: one big region of the page colour."""

        return (self.fraction >= LOGIN_PAGE_MIN_FRACTION
                and self.area >= LOGIN_PAGE_MIN_PIXELS)


WHEEL_DELTA = 120
WHEEL_METHODS = ("sendinput", "postmessage", "mouse_event")


def wheel_send_input(x: int, y: int, notches: int) -> bool:
    """Scroll with ``SendInput`` (the modern replacement for mouse_event)."""

    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class INPUT(ctypes.Structure):
        class _VALUE(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        _anonymous_ = ("value",)
        _fields_ = [("type", wintypes.DWORD), ("value", _VALUE)]

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    inputs = (INPUT * 1)()
    inputs[0].type = 0                                   # INPUT_MOUSE
    inputs[0].mi.dx = 0
    inputs[0].mi.dy = 0
    inputs[0].mi.mouseData = wintypes.DWORD((-int(notches) * WHEEL_DELTA) & 0xFFFFFFFF)
    inputs[0].mi.dwFlags = 0x0800                        # MOUSEEVENTF_WHEEL
    inputs[0].mi.time = 0
    sent = user32.SendInput(1, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return bool(sent)


def wheel_post_message(x: int, y: int, notches: int, hwnd: int = 0) -> bool:
    """Scroll with ``WM_MOUSEWHEEL`` posted to the window under the cursor.

    A game whose channel list is a normal GDI/listbox control reads exactly this message, while it may
    ignore a synthetic hardware wheel - which is what the field log showed.
    """

    import win32api
    import win32con
    import win32gui

    target = int(hwnd) or win32gui.WindowFromPoint((int(x), int(y)))
    if not target:
        return False
    delta = -int(notches) * WHEEL_DELTA
    wparam = (delta & 0xFFFF) << 16
    lparam = (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)
    win32api.PostMessage(target, win32con.WM_MOUSEWHEEL, wparam, lparam)
    return True


def wheel_legacy(x: int, y: int, notches: int) -> bool:
    """The legacy ``mouse_event`` wheel (kept as the last resort)."""

    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    user32.mouse_event(0x0800, 0, 0, -int(notches) * WHEEL_DELTA, 0)
    return True


def wheel_screen(x: int, y: int, notches: int, *, method: str = "sendinput",
                 hwnd: int = 0) -> bool:
    """Scroll the mouse wheel at a screen point with ONE method (positive = down)."""

    try:
        if method == "postmessage":
            return wheel_post_message(x, y, notches, hwnd)
        if method == "mouse_event":
            return wheel_legacy(x, y, notches)
        return wheel_send_input(x, y, notches)
    except Exception:
        LOG.warning("auto reconnect: the %s wheel at (%s, %s) failed", method, x, y, exc_info=True)
        return False


def _mouse_structs():
    """The Win32 ``INPUT``/``MOUSEINPUT`` pair for ``SendInput``."""

    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.WPARAM)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    return MOUSEINPUT, INPUT


def _absolute_mouse_xy(user32, x: int, y: int) -> tuple[int, int]:
    """Screen pixels -> the 0..65535 absolute coordinates ``SendInput`` wants.

    Normalised against the VIRTUAL desktop (all monitors, origin included), because that is the space
    ``MOUSEEVENTF_VIRTUALDESK`` addresses - the same conversion the trade clicks use.
    """

    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1)
    height = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1)
    return (round((int(x) - left) * 65535 / width), round((int(y) - top) * 65535 / height))


def _warn_if_the_cursor_did_not_move(x: int, y: int) -> None:
    """Say so when the pointer is not where the click asked for it (it would click elsewhere)."""

    try:
        import ctypes
        from ctypes import wintypes

        where = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(where)):
            if (int(where.x), int(where.y)) != (int(x), int(y)):
                LOG.warning("auto reconnect: the cursor is at (%d, %d) although the click asked for "
                            "(%d, %d) - the pointer did not move where it should",
                            int(where.x), int(where.y), int(x), int(y))
    except Exception:
        LOG.debug("auto reconnect: the cursor position could not be read back", exc_info=True)


def send_input_click(x: int, y: int, *, settle: float = CLICK_SETTLE_SECONDS,
                     hold: float = CLICK_HOLD_SECONDS) -> bool:
    """One left click at a screen point injected with ``SendInput`` -> True when it was delivered.

    ``SendInput`` (not the legacy ``mouse_event``) is what every other input in this project uses -
    the keys, the trade clicks and the wheel - and the field measurement behind the wheel method list
    is that **this game ignores ``mouse_event``**.  That is exactly the channel-cell symptom the
    operator reported: "i can see that the mouse is double clicking but it just cannot enter that
    channel, however my manual operation is good" - the pointer was placed on the cell (so the game
    showed the hover, and the cell looked selected) while the button events never reached it.

    The move, the press and the release are three separate injected events, so ``settle`` (after the
    move) and ``hold`` (the button down time) can be given a human's timing.
    """

    import ctypes

    MOUSEINPUT, INPUT = _mouse_structs()
    user32 = ctypes.windll.user32
    dx, dy = _absolute_mouse_xy(user32, x, y)
    events = (INPUT(0, MOUSEINPUT(dx, dy, 0, MOUSE_MOVE_ABSOLUTE, 0, 0)),
              INPUT(0, MOUSEINPUT(dx, dy, 0, MOUSE_MOVE_ABSOLUTE | MOUSE_LEFT_DOWN, 0, 0)),
              INPUT(0, MOUSEINPUT(dx, dy, 0, MOUSE_MOVE_ABSOLUTE | MOUSE_LEFT_UP, 0, 0)))
    try:
        sent = user32.SendInput(1, ctypes.byref(events[0]), ctypes.sizeof(INPUT))
        if sent != 1:
            return False
        if settle > 0:
            time.sleep(settle)
        _warn_if_the_cursor_did_not_move(x, y)
        sent = user32.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(INPUT))
        if sent != 1:
            return False
        time.sleep(max(0.0, hold))
        sent = user32.SendInput(1, ctypes.byref(events[2]), ctypes.sizeof(INPUT))
        return sent == 1
    except Exception:
        LOG.debug("auto reconnect: SendInput click failed", exc_info=True)
        return False


def mouse_event_click(x: int, y: int, *, settle: float = CLICK_SETTLE_SECONDS,
                      hold: float = CLICK_HOLD_SECONDS) -> bool:
    """The legacy ``mouse_event`` click - only the fallback when ``SendInput`` refuses."""

    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        if settle > 0:
            time.sleep(settle)
        _warn_if_the_cursor_did_not_move(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)          # LEFTDOWN
        time.sleep(max(0.0, hold))
        user32.mouse_event(0x0004, 0, 0, 0, 0)          # LEFTUP
        return True
    except Exception:
        LOG.exception("auto reconnect: click at (%s, %s) failed", x, y)
        return False


def click_screen(x: int, y: int, *, keep_focus: int = 0, settle: float = CLICK_SETTLE_SECONDS,
                 hold: float = CLICK_HOLD_SECONDS, method: str = "sendinput") -> bool:
    """A real left click at a screen position.

    ``keep_focus`` is the game's window handle: it is made the ACTIVE window in the last moment
    before the mouse events, because a game ignores mouse clicks while it is not active - the
    measured reason a click can land on the right pixel and still do nothing.  ``settle`` (wait after
    the cursor is placed) and ``hold`` (how long the button stays down) are separate so a
    double-click can be sent with a human's timing: the second click of a double click does not need
    to settle the cursor again, and a 30 ms hold may be too short for a game that samples the button.
    The click itself goes through :func:`send_input_click`; ``mouse_event`` is only the fallback.
    """

    try:
        if keep_focus:
            try:
                import win32gui

                if win32gui.GetForegroundWindow() != int(keep_focus):
                    win32gui.SetForegroundWindow(int(keep_focus))
                    time.sleep(0.05)
            except Exception:
                LOG.debug("auto reconnect: the focus could not be taken just before the click",
                          exc_info=True)
        if method == "mouse_event":
            return mouse_event_click(x, y, settle=settle, hold=hold)
        if send_input_click(x, y, settle=settle, hold=hold):
            return True
        LOG.warning("auto reconnect: SendInput could not inject the click at (%d, %d) - falling back "
                    "to mouse_event, which this game is known to ignore", x, y)
        return mouse_event_click(x, y, settle=settle, hold=hold)
    except Exception:
        LOG.exception("auto reconnect: click at (%s, %s) failed", x, y)
        return False


def find_connect_button(frame, board_box: Optional[tuple[int, int, int, int]] = None, *,
                        template=None):
    """Locate the 连接 button on the login board -> (x, y, width, height) or None.

    Two ways, in order:

    1. **template** (optional): ``recording-assets/login_connect_target.jpg``, matched with
       ``cv2.matchTemplate`` and returned when the best score clears
       :data:`CONNECT_TEMPLATE_MIN_SCORE`.  This is the exact-art route: once a crop of the real
       button is shipped, the position is read off the art itself.
    2. **shape**: the board is flat cream, so the button is the largest *saturated filled shape*
       on it that has a button-like size and aspect (see the ``CONNECT_BUTTON_*`` constants) and
       sits in the lower two thirds of the board.

    Never raises: an unusable frame returns None, and the caller then keeps pressing Enter.
    """

    import cv2
    import numpy as np

    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]

    if template is not None:
        try:
            # TM_SQDIFF_NORMED, not the correlation: the login board is flat cream, so a
            # correlation scores every flat region ~1.0 (measured: min == max at (0, 0) for an
            # exact crop on the real art).  The squared difference is 0.0 exactly at the match.
            result = cv2.matchTemplate(frame, template, cv2.TM_SQDIFF_NORMED)
            min_value, _max_value, min_location, _max_location = cv2.minMaxLoc(result)
            quality = 1.0 - float(min_value)
            if quality >= CONNECT_TEMPLATE_MIN_SCORE:
                template_h, template_w = template.shape[:2]
                LOG.info("auto reconnect: 连接 button found by template at (%d, %d) "
                         "(difference %.3f, quality %.2f)", min_location[0], min_location[1],
                         min_value, quality)
                return (int(min_location[0]), int(min_location[1]),
                        int(template_w), int(template_h))
            LOG.info("auto reconnect: 连接 template best difference %.3f (quality %.2f) is below "
                     "the threshold", min_value, quality)
        except Exception:
            LOG.debug("auto reconnect: template matching failed", exc_info=True)

    if board_box is not None:
        left, top, box_width, box_height = (int(value) for value in board_box)
    else:
        left, top, box_width, box_height = 0, 0, width, height
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    box_width = max(8, min(box_width, width - left))
    box_height = max(8, min(box_height, height - top))
    crop = frame[top:top + box_height, left:left + box_width]

    try:
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 1] >= CONNECT_BUTTON_SATURATION_MIN)
                & (hsv[:, :, 2] >= CONNECT_BUTTON_VALUE_MIN)).astype(np.uint8)
    except Exception:
        LOG.debug("auto reconnect: 连接 candidate mask failed", exc_info=True)
        return None
    # close small holes (the button's own text punches gaps into the fill)
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    board_area = float(box_width * box_height)
    best = None
    best_area = 0
    for index in range(1, count):
        x, y, item_w, item_h, area = (int(value) for value in stats[index])
        if area < CONNECT_BUTTON_MIN_AREA_RATIO * board_area:
            continue
        if not (CONNECT_BUTTON_WIDTH_RANGE[0] * box_width <= item_w
                <= CONNECT_BUTTON_WIDTH_RANGE[1] * box_width):
            continue
        if not (CONNECT_BUTTON_HEIGHT_RANGE[0] * box_height <= item_h
                <= CONNECT_BUTTON_HEIGHT_RANGE[1] * box_height):
            continue
        aspect = item_w / float(max(1, item_h))
        if not (CONNECT_BUTTON_ASPECT_RANGE[0] <= aspect <= CONNECT_BUTTON_ASPECT_RANGE[1]):
            continue
        # the button is in the lower part of the board, in the middle column
        centre_x = x + item_w / 2.0
        centre_y = y + item_h / 2.0
        if not (0.25 * box_width <= centre_x <= 0.75 * box_width):
            continue
        if centre_y < 0.35 * box_height:
            continue
        LOG.info("auto reconnect: 连接 candidate client box=(%d, %d, %d, %d) area=%d fill=%.2f",
                 left + x, top + y, item_w, item_h, area,
                 area / float(max(1, item_w * item_h)))
        if area > best_area:
            best_area = area
            best = (left + x, top + y, item_w, item_h)
    if best is None:
        LOG.info("auto reconnect: no 连接 button candidate on the board (%dx%d)",
                 box_width, box_height)
    return best


def point_in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    """Whether a screen point lies inside a (left, top, right, bottom) rectangle."""

    left, top, right, bottom = (int(value) for value in rect)
    return left <= int(x) <= right and top <= int(y) <= bottom


def point_in_box(x: int, y: int, box: tuple[int, int, int, int]) -> bool:
    """Whether a client point lies inside a (left, top, width, height) box."""

    left, top, width, height = (int(value) for value in box)
    return left <= int(x) <= left + width and top <= int(y) <= top + height


def screen_change(before, after, *,
                  size: tuple[int, int] = SCREEN_CHANGE_THUMBNAIL) -> float:
    """How much two frames differ (0 when identical, 255 when opposite).

    Compared on a coarse grey thumbnail: a real screen change (login page -> world list) is a
    large number, a blinking caret or a moving particle is a small one.  Never raises - an
    unreadable frame pair returns 0.0, i.e. "nothing happened".
    """

    import cv2
    import numpy as np

    if before is None or after is None:
        return 0.0
    try:
        if before.shape[:2] != after.shape[:2]:
            return float(SCREEN_CHANGE_MIN)          # a resized window certainly changed
        first = cv2.resize(cv2.cvtColor(before, cv2.COLOR_BGR2GRAY), size,
                           interpolation=cv2.INTER_AREA)
        second = cv2.resize(cv2.cvtColor(after, cv2.COLOR_BGR2GRAY), size,
                            interpolation=cv2.INTER_AREA)
        return float(np.abs(first.astype(np.int16) - second.astype(np.int16)).mean())
    except Exception:
        LOG.debug("auto reconnect: screen comparison failed", exc_info=True)
        return 0.0


def crop_client_region(frame, box: tuple[int, int, int, int]):
    """The (left, top, width, height) CLIENT region of a frame, clamped to the frame."""

    if frame is None:
        return None
    height, width = frame.shape[:2]
    left, top, box_width, box_height = (int(value) for value in box)
    x0 = max(0, min(left, width))
    y0 = max(0, min(top, height))
    x1 = max(x0, min(left + box_width, width))
    y1 = max(y0, min(top + box_height, height))
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]


def region_change(before, after, box: tuple[int, int, int, int]) -> float:
    """``screen_change`` over ONE client region, so a small move inside it is not averaged away.

    The channel list shifts by one row (30 px) per wheel notch.  On a 64x36 thumbnail of the whole
    window that is invisible; on a thumbnail of the list region alone it is a clear difference.
    """

    return screen_change(crop_client_region(before, box), crop_client_region(after, box))


def list_scroll_rows(before, after, *, box: tuple[int, int, int, int] = CHANNEL_LIST_CLIENT_BOX,
                     pitch: float = CHANNEL_ROW_STEP_CLIENT) -> Optional[float]:
    """How many rows the channel list moved down between two frames (None: not measurable).

    A strip of the *later* list is searched in the *earlier* one: content that moved up by ``rows *
    pitch`` px is found that much lower in the earlier frame, so the match position gives the shift.
    The strip keeps three rows of margin, which covers "nothing moved" up to two rows at once - more
    than a single wheel notch can do.
    """

    import cv2

    first = crop_client_region(before, box)
    second = crop_client_region(after, box)
    if first is None or second is None:
        return None
    step = max(1, int(round(pitch)))
    height = second.shape[0]
    top = step * CHANNEL_SCROLL_MATCH_ROWS
    bottom = height - step * 2
    if bottom - top < step:
        return None
    try:
        template = cv2.cvtColor(second[top:bottom], cv2.COLOR_BGR2GRAY)
        window = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
        if template.shape[0] >= window.shape[0] or template.shape[1] > window.shape[1]:
            return None
        score = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
        _min, best, _min_loc, best_loc = cv2.minMaxLoc(score)
    except Exception:
        LOG.debug("auto reconnect: the list shift could not be measured", exc_info=True)
        return None
    if best < CHANNEL_SCROLL_MATCH_MIN_SCORE:
        LOG.info("auto reconnect: the list shift is not measurable (best match %.2f at y=%d)",
                 best, best_loc[1])
        return None
    shift = int(best_loc[1]) - top
    LOG.info("auto reconnect: the list moved %d px (%.2f rows, match %.2f)", shift,
             shift / float(pitch), best)
    return shift / float(pitch)


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


class _FocusWatch(threading.Thread):
    """Sample the foreground window during a run and log every CHANGE.

    The operator: *"there's must be sth that steal focus of the game thus causing login board click
    failed"*.  A game ignores clicks while it is not the active window, so WHO takes the focus is the
    missing piece of evidence - this names it (title, hwnd, process id, and whether that process is
    the assistant itself) and counts the thefts for the run report.
    """

    INTERVAL_SECONDS = 0.1

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="auto-reconnect-focus-watch", daemon=True)
        self._app_stop = stop_event
        self._stop = threading.Event()
        self.changes: list = []
        self._current: Optional[int] = None

    def close(self) -> None:
        """Stop this one reconnect-run diagnostic watcher."""

        self._stop.set()

    def sample(self) -> Optional[int]:
        """The foreground window handle right now (None when it cannot be read)."""

        try:
            import win32gui

            return int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            return None

    def describe(self, hwnd: int) -> str:
        try:
            import win32gui

            title = win32gui.GetWindowText(hwnd) or win32gui.GetClassName(hwnd)
            pid = window_process_id(hwnd)
            who = "助手自己" if pid and pid == os.getpid() else "别的程序"
            return f"「{title}」 hwnd {hwnd} pid {pid} ({who})"
        except Exception:
            return f"hwnd {hwnd}"

    def run(self) -> None:
        self._current = self.sample()
        while not self._app_stop.is_set() and not self._stop.is_set():
            if self._stop.wait(self.INTERVAL_SECONDS):
                return
            if self._app_stop.is_set():
                return
            now = self.sample()
            if now is None or now == self._current:
                continue
            previous, self._current = self._current, now
            line = (f"foreground changed: {self.describe(previous)} -> {self.describe(now)}")
            self.changes.append(line)
            LOG.warning("auto reconnect: %s", line)

    def summary(self) -> str:
        """A short account of the thefts (for the panel)."""

        if not self.changes:
            return "焦点没有被抢"
        return f"焦点被抢 {len(self.changes)} 次: " + " | ".join(self.changes[-3:])


class ReconnectWorker(threading.Thread):
    """Watch for a confirmed disconnect and log the character back in."""

    def __init__(
        self,
        key_sender: Any,
        stop_event: threading.Event,
        result_queue: "queue.Queue[tuple[str, str]]",
        *,
        window_title: str = "",
        capture_fn: Optional[Callable[[], Any]] = None,
        template_path: Optional[Path] = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        client_size_fn: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
        click_fn: Optional[Callable[[int, int], bool]] = None,
        wheel_fn: Optional[Callable[[int, int, int], bool]] = None,
        point_owner_fn: Optional[Callable[[int, int], Optional[tuple[int, str, str]]]] = None,
        page_fn: Optional[Callable[[], tuple[Optional[str], float]]] = None,
        post_login_marker_ready_fn: Optional[Callable[[], bool]] = None,
        automation_event: Optional[threading.Event] = None,
        reconnect_active_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="auto-reconnect-worker", daemon=True)
        self.key_sender = key_sender
        self.stop_event = stop_event
        self.result_queue = result_queue
        self.window_title = str(window_title)
        self.dry_run = bool(dry_run)
        self._sleep = sleep
        self._capture_fn = capture_fn
        self._template_path = template_path
        self._client_size_fn = client_size_fn
        # The activation click (focus -> click -> Enter).  Tests inject their own so nothing
        # moves the real mouse; --dry-run only logs it.
        self._click_fn = click_fn
        self._wheel_fn = wheel_fn
        # Which window owns a screen point.  Measured in the field: the assistant window kept
        # taking the foreground back, which happens when a click lands on ANOTHER window - a click
        # there activates that window instead of the game.  Tests inject their own.
        self._point_owner_fn = point_owner_fn
        # The automation switch (patrol/attack).  The reconnect needs live input for its own keys,
        # and that same switch is what wakes the attack worker, so the automation is stood down for
        # the duration of a run: measured in the field (19:07) the attack worker pressed `a` every
        # second through the whole sequence.
        self.automation_event = automation_event
        # Raised for the whole run: the focus gate keeps the automation switch cleared while it is
        # set, so patrol/attack cannot resume on the login page.
        self.reconnect_active_event = reconnect_active_event
        # 掉线提示窗口 (see OFFLINE_PROMPT_ENTER): one Enter right after the login page is recognized.
        self.dismiss_offline_prompt = OFFLINE_PROMPT_CLICK or OFFLINE_PROMPT_ENTER
        # The precise reason the last click could not be sent (ownership, delivery, ...).  The
        # step that asked for the click reports it ONCE - the field log showed a precise failure
        # followed by a vague "点击登录面板失败" for the very same cause.
        self._click_failure: Optional[str] = None
        # The focus watchdog (v0408): it names the window that steals the foreground while a run is
        # going on, because a game ignores clicks while it is not the active window.
        self._focus_watch: Optional[_FocusWatch] = None
        # Which wheel method actually moves the channel list.  The field log (19:07) showed the
        # legacy mouse_event wheel being ignored by the game, so the methods are tried in turn and
        # the first one that MOVES the list is kept for the rest of the run.
        self._wheel_method = WHEEL_METHODS[0]
        # The window whose client area produced the captured frame.  Every geometry check uses THIS
        # one, so the picture and the clicks can never come from two different windows.
        self._frame_hwnd = 0
        self._reference: Optional[LoginColourReference] = None
        self._reference_loaded = False
        self._offline_prompt_square = None
        self._offline_prompt_square_loaded = False
        self._connect_template_image = None
        self._connect_template_loaded = False
        # The size of the last captured frame: the click constants are mapped from the 1366x768
        # preset space onto it (``_click_size``), without capturing again inside a measurement loop.
        self._last_frame_size: Optional[tuple[int, int]] = None
        # The page signatures (login board / world grid / channel grid) and the page the last
        # measurement saw.  `page_fn` is the test seam: it returns (page, score) directly.
        self._page_references_loaded = False
        self._page_references: dict = {}
        self._page_fn = page_fn
        # The application owns minimap geometry and injects this fresh-frame
        # marker probe.  Keeping it here as a callback prevents reconnect from
        # maintaining a second, conflicting minimap detector.
        self._post_login_marker_ready_fn = post_login_marker_ready_fn

        self._lock = threading.Lock()
        self._enabled = False
        self._world = WORLD_DEFAULT
        self._channel = CHANNEL_DEFAULT
        self._wake = threading.Event()
        # A reconnect is armed for the whole enabled session.  This only marks an
        # event that has been queued but has not reached the worker loop yet; it
        # must never become a permanent "already reconnected" latch.
        self._disconnect_queued = False
        self._login_entered = False
        # Set by trigger_test(): the temporary 测试重连 button runs the sequence once even
        # when the enable checkbox is still off, so the operator can try it before trusting
        # the automatic 掉线 trigger.
        self._test_requested = False
        self._testing = False
        self._running = False
        self._cancel_requested = threading.Event()

    # ------------------------------------------------------------------ settings

    def set_enabled(self, enabled: bool) -> None:
        """Arm or disarm the worker - ticking the box never starts a run.

        The operator's rule: the selection only makes the worker ready.  The drill starts on
        the 掉线 event or when the 测试自动重连 button is clicked.  (Waking the loop from here once
        ran the whole sequence the moment the box was ticked.)
        """

        with self._lock:
            self._enabled = bool(enabled)
            self._disconnect_queued = False
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
            if not self._enabled:
                LOG.info("auto reconnect: disconnect ignored because it is disabled")
                return
            if self._running or self._disconnect_queued or self._testing or self._test_requested:
                LOG.info("auto reconnect: duplicate disconnect ignored; a reconnect is already queued/running")
                return
            # A successful earlier run must not suppress a genuinely new offline
            # event hours later.  Reset the historical marker for this new cycle.
            self._disconnect_queued = True
            self._login_entered = False
        LOG.info("auto reconnect: disconnect event seen; checking the game window")
        self._wake.set()

    def trigger_test(self) -> bool:
        """Run the whole sequence once from the panel's temporary 测试重连 button.

        The button exists to try the function *before* trusting it: the enable checkbox is
        therefore ignored for this run, and a finished run re-arms so the button can be
        pressed again.  False means a run is already going on.
        """

        with self._lock:
            if self._test_requested or self._testing or self._running or self._disconnect_queued:
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

    def is_active(self) -> bool:
        """Whether a reconnect currently owns the game and may be cancelled."""

        with self._lock:
            return bool(self._running)

    def request_cancel(self) -> bool:
        """Stop the current reconnect at its next safe wait/checkpoint."""

        with self._lock:
            if not self._running:
                return False
        self._cancel_requested.set()
        self._wake.set()
        LOG.warning("auto reconnect: cancellation requested by Esc")
        self._report("cancelled", "Esc 已取消自动重连")
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
        with self._lock:
            testing_requested = self._test_requested
            # The queued flag protects only the handoff from notify_disconnect()
            # to this loop iteration.  Once consumed, a later real disconnect can
            # be handled after this run completes.
            self._disconnect_queued = False
        if not enabled and not testing_requested:
            return
        testing = self._take_test_request()
        with self._lock:
            self._running = True
            self._cancel_requested.clear()
        try:
            self._run_sequence(world, channel, testing)
        finally:
            with self._lock:
                self._running = False
                self._testing = False

    def _run_sequence(self, world: str, channel: int, testing: bool) -> None:
        if testing:
            LOG.info("auto reconnect: manual test run (ignoring the enable checkbox)")
        # FIRST, before anything else: the automation must not type into a game that is on the login
        # page.  It used to happen only after the colour gate below, which waits up to 12 x 1 s - the
        # operator's "why is that patrol is active?  it shouldn't be activated on reconnect stage".
        automation_was = self._stand_down_automation()
        self._take_keyboard()
        succeeded = False
        try:
            succeeded = self._run_verified(world, channel, testing)
        finally:
            self._release_keyboard()
            self._finish_automation(automation_was, succeeded)
        return

    def _finish_automation(self, automation_was: bool, succeeded: bool) -> None:
        """Hand the machine back after a run - the automation only resumes on SUCCESS.

        The focus gate re-arms the automation by itself whenever live input is armed and the game is
        foreground, so after a FAILED run the input is disarmed first (see `_run_verified`); only then
        is the reconnect flag lowered, and only a successful login re-arms the automation explicitly.
        """

        cancelled = self._cancel_requested.is_set()
        if cancelled:
            LOG.warning("auto reconnect: cancelled by Esc - patrol stays stopped")
        elif not succeeded:
            LOG.error("auto reconnect: the login did NOT succeed - patrol stays stopped, it must not "
                      "run on the login page (character is not in game)")
            self._report("failed", "登录没有成功：已停止巡逻（不会在登录页乱按键），"
                                   "请手动处理后再开始巡逻")
        if self.reconnect_active_event is not None:
            try:
                self.reconnect_active_event.clear()
                LOG.info("auto reconnect: the focus gate may manage the automation again")
            except Exception:
                LOG.debug("auto reconnect: the reconnect flag could not be lowered", exc_info=True)
        # one focus-gate poll with the flag down, so the gate settles on the real input state
        self._sleep(0.4)
        # On SUCCESS the patrol has to be prepared again, not just switched back on: detecting the
        # layer from the marker, re-anchoring the map session and queueing the startup floor is what
        # 开始巡逻 does (assistant.prepare_map_session -> movement_worker.prepare_patrol_start), and it is
        # what lets the movement worker resync onto the patrol route after a reconnect - the operator's
        # v1.0.25 report: "it don't start patrol, it didn't go into detect layer and check if back to
        # patrol route logic ... what i want is that i don't need to manually restart patrol".
        #
        # It does NOT depend on `automation_was` (the disconnect alert and the focus gate stop a
        # running patrol as soon as the 掉线提示 window takes the foreground, LONG before this run
        # starts: "the automation was not running before the run - left as it is"), and it does NOT
        # depend on how the run was started either: a 测试重连 run is the operator's own way of running
        # the sequence, and in v1.0.29 was exactly the case where the patrol was left stopped even
        # though the reconnect succeeded ("failed again" - the log said "manual test run with the
        # automation down - the patrol is not started by the test").  The assistant owns the decision:
        # it resumes only a patrol that the disconnect actually interrupted.
        if succeeded and not cancelled:
            if not self._wait_for_post_login_marker():
                self._disarm_input()
                self._report(
                    "failed",
                    "已登录但未连续检测到黄色角色标记；巡逻未恢复",
                )
                self._announce_disarmed_input()
                return
            # The gate flag goes down FIRST: the restart below arms the input again, and the focus gate
            # must not be holding the automation "paused for the auto-reconnect" while it does.
            self._restore_automation(automation_was)
            self._report("patrol-restart", "重连完成：黄色角色标记已连续确认，重新检测层数并恢复巡逻")
        elif automation_was:
            self._restore_automation(True)
        elif succeeded:
            LOG.info("auto reconnect: the automation was not running before the run - left as it is")
        # Every path ends here, so this is where the operator is told that the assistant cannot type
        # any more: with live input OFF every hotkey that sends keys is refused (and used to say so
        # only at DEBUG level, which is what made it look like a lost hotkey binding).
        self._announce_disarmed_input()

    def _wait_for_post_login_marker(self) -> bool:
        """Wait for consecutive fresh yellow-marker samples after channel entry.

        The callback is deliberately supplied by ``assistant.py``: it captures
        a fresh shared frame and uses the application's one minimap coordinate
        system.  Reconnect only owns the timing and confirmation policy.
        """

        probe = self._post_login_marker_ready_fn
        if not callable(probe):
            # Compatibility seam for isolated callers.  The real application
            # always injects the probe below.
            LOG.warning("auto reconnect: no post-login marker probe; skipping readiness gate")
            return True
        self._report("loading", "等待黄色角色标记连续出现")
        deadline = time.monotonic() + POST_LOGIN_MARKER_TIMEOUT_SECONDS
        consecutive = 0
        samples = 0
        while time.monotonic() < deadline:
            if self.stop_event.is_set() or self._cancel_requested.is_set():
                return False
            samples += 1
            visible = self._post_login_marker_visible()
            consecutive = consecutive + 1 if visible else 0
            if consecutive >= POST_LOGIN_MARKER_CONFIRM_SAMPLES:
                LOG.info(
                    "auto reconnect: yellow marker confirmed %d/%d consecutive samples; "
                    "restarting patrol immediately",
                    consecutive,
                    POST_LOGIN_MARKER_CONFIRM_SAMPLES,
                )
                return True
            if not self._sleep_checked(POST_LOGIN_MARKER_POLL_SECONDS):
                return False
        LOG.error(
            "auto reconnect: yellow marker did not remain visible for %d consecutive samples "
            "within %.1fs (%d probes)",
            POST_LOGIN_MARKER_CONFIRM_SAMPLES,
            POST_LOGIN_MARKER_TIMEOUT_SECONDS,
            samples,
        )
        return False

    def _post_login_marker_visible(self) -> bool:
        """Return one safe fresh yellow-marker observation, if available."""

        probe = self._post_login_marker_ready_fn
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            LOG.debug("auto reconnect: post-login marker probe failed", exc_info=True)
            return False

    def _wait_for_channel_confirm_or_marker(self, seconds: float) -> Optional[bool]:
        """Wait for a channel handoff window, unless the game is already live.

        ``True`` means a character marker was seen and the remaining Enter
        confirmation must be skipped; ``False`` means the quiet interval
        elapsed; ``None`` means stop/Esc interrupted the reconnect.
        """

        # Test clocks intentionally do not advance ``time.monotonic()``.  Use
        # their supplied one-shot wait while retaining the real application's
        # responsive 5 fps marker polling below.
        if self._sleep is not time.sleep:
            if self._post_login_marker_visible():
                return True
            if not self._sleep_checked(seconds):
                return None
            return self._post_login_marker_visible()

        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.stop_event.is_set() or self._cancel_requested.is_set():
                return None
            if self._post_login_marker_visible():
                LOG.info(
                    "auto reconnect: yellow marker appeared during channel confirmation; "
                    "skipping remaining Enter pairs"
                )
                self._report("loading", "角色已进入地图，停止后续 Enter 确认")
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not self._sleep_checked(
                    min(CHANNEL_CONFIRM_MARKER_POLL_SECONDS, remaining)):
                return None

    def _announce_disarmed_input(self) -> None:
        """State loudly that typing is off after this run, in the log and in the panel."""

        state = self._input_state()
        if state is None or state:
            return
        LOG.error(
            "auto reconnect: live input is OFF after this run - every key the assistant sends is "
            "refused until 开始巡逻/Start Patrol (typing hotkeys re-arm it by themselves)"
        )
        self._report(
            "input",
            "自动重连结束：实时输入已关闭（不会在登录页乱按键）。按 开始巡逻 后按键才恢复。",
        )

    def _run_verified(self, world: str, channel: int, testing: bool) -> bool:
        """The sequence itself -> True only when the character really got in."""

        if not self._prepare_window():
            self._report("failed", "game window is not available")
            return False
        self._check_frame_size()
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
            return False
        # From here the sequence really sends input: arm live input if the operator has not
        # (the default), and put that state back when the sequence ends either way.
        previous_input = self._arm_input()
        succeeded = False
        watch = _FocusWatch(self.stop_event)
        self._focus_watch = watch
        if not self.dry_run:
            try:
                watch.start()
            except Exception:
                LOG.debug("auto reconnect: the focus watchdog could not be started", exc_info=True)
        try:
            # The operator's order: focus the window, click inside it (the game ignores keys
            # until it has seen a click), then click 连接 when it can be located, and otherwise
            # use Enter.  Nothing else on the login board is ever clicked.
            if not self._activate_window():
                return False
            if not self._dismiss_offline_prompt():
                return False
            if not self._enter_game():
                return False
            if not self._select_world(world):
                return False
            if not self._select_channel(channel):
                return False
            with self._lock:
                self._login_entered = True
            self._report("done", f"{world} {channel}频道")
            LOG.info("auto reconnect: focus summary - %s", self._focus_summary())
            succeeded = True
            return True
        finally:
            if succeeded:
                self._restore_input(previous_input)
            else:
                # A failed login must not leave the assistant patrolling on the login page: disarm the
                # input (the patrol state), which also makes the focus gate keep the automation
                # cleared by itself.
                self._disarm_input()
            # This diagnostic belongs to ONE reconnect run. Leaving it alive
            # made it keep watching foreground changes for the rest of the
            # application lifetime, including the hidden FFmpeg diagnostic
            # encoder that runs after a disconnect screenshot recording.
            try:
                watch.close()
                if watch.is_alive():
                    watch.join(timeout=0.25)
            except Exception:
                LOG.debug("auto reconnect: focus watcher could not be stopped", exc_info=True)
            self._focus_watch = None

    def _check_frame_size(self) -> None:
        """Say out loud what the captured frame measures - every click constant assumes 1366x768.

        Measured on the operator's machine: the display runs at **150 %** scaling and this process
        is DPI-unaware, so the game client really is 1366x768 in the virtualised desktop the clicks
        are sent to, and no scaling arithmetic belongs in the click path.  Should the capture ever
        come back at another size, the shipped points would describe a different picture, so that
        is logged as a warning instead of turning into a click that lands somewhere else.
        """

        frame, rect = self._capture()
        if frame is None:
            LOG.warning("auto reconnect: no frame to measure the client size with")
            return
        # The whole click mapping in one line: client origin on screen, foreground window, game
        # handle.  Every click below is "origin + client point", so this is what a wrong mapping
        # would show.
        candidates = find_game_windows(self.window_title)
        for item in candidates:
            pid = window_process_id(item["hwnd"])
            LOG.info("auto reconnect: game window candidate hwnd %s class %s pid %s exe %s rect %s "
                     "client %s at (%d, %d)%s", item["hwnd"], item["class"], pid,
                     process_image_name(pid) or "?", item["rect"], item["client_size"],
                     item["client_origin"][0], item["client_origin"][1],
                     " (minimized)" if item["minimized"] else "")
        frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        matches = [item for item in candidates if item["client_size"] == frame_size]
        if matches:
            self._frame_hwnd = int(matches[0]["hwnd"])
            LOG.info("auto reconnect: the captured %dx%d frame belongs to hwnd %s (%d candidate(s) "
                     "with this title) - every click is computed from that window",
                     frame_size[0], frame_size[1], self._frame_hwnd, len(candidates))
        elif candidates:
            LOG.error("auto reconnect: the captured frame is %dx%d but no window titled %r has a "
                      "client of that size (%s) - the picture and the clicks would come from "
                      "DIFFERENT windows", frame_size[0], frame_size[1], self.window_title,
                      ", ".join(f"hwnd {item['hwnd']} client {item['client_size']}"
                                for item in candidates))
        LOG.info("auto reconnect: client origin on screen (%d, %d); captured frame %dx%d; foreground "
                 "window %r; game hwnd %s (frame hwnd %s)", int(rect[0]), int(rect[1]),
                 frame_size[0], frame_size[1], self._foreground_title(), self._game_hwnd(),
                 self._frame_hwnd)
        height, width = frame.shape[:2]
        if (width, height) == tuple(REFERENCE_CLIENT):
            LOG.info("auto reconnect: the game frame is %dx%d - the space every click is measured "
                     "in", width, height)
            return
        scale = window_client_scale((width, height))
        LOG.warning("auto reconnect: the game frame is %dx%d while every click constant is measured "
                    "in %dx%d - the points are mapped onto this client before clicking "
                    "(login board: centred, offset %+d px; lists: x %.3f, y about the middle), so "
                    "the picture and the clicks agree", width, height,
                    REFERENCE_CLIENT[0], REFERENCE_CLIENT[1],
                    int(round((width - REFERENCE_CLIENT[0]) / 2.0)), round(scale, 3))

    def _game_hwnd(self) -> int:
        """The game's window handle.

        The window that produced the captured frame wins, because the click coordinates are measured in
        THAT window's client area (v0415): with two windows sharing the title, the sender's handle could
        belong to the other one, which put every click at the wrong screen position.
        """

        if self._frame_hwnd:
            return int(self._frame_hwnd)
        sender = self.key_sender
        for attribute in ("hwnd", "_hwnd", "window_handle"):
            hwnd = getattr(sender, attribute, None) if sender is not None else None
            if hwnd:
                try:
                    return int(hwnd)
                except Exception:
                    pass
        if not self.window_title:
            return 0
        try:
            import win32gui

            return int(win32gui.FindWindow(None, self.window_title) or 0)
        except Exception:
            LOG.debug("auto reconnect: the game window handle could not be looked up",
                      exc_info=True)
            return 0

    def _foreground_title(self) -> str:
        """The title of the window that has the foreground right now."""

        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            return win32gui.GetWindowText(hwnd) or f"hwnd {hwnd}"
        except Exception:
            return "?"

    def _point_owner(self, x: int, y: int):
        """The window at a screen point when a click there must NOT be sent -> ``(hwnd, name)``.

        Exactly ONE thing is refused: a window of the **assistant's own process**, because clicking it
        activates the assistant and the game never sees the click ("the window keeps losing focus to
        assistant, so that the click is not successfully emitted to the game").

        Everything else is accepted.  The field logs (19:40, 19:46) showed why every stricter rule
        failed: the login board and 连接 are drawn in `igwUserLoginDialog`, which is *not* the game's
        main window - and the operator confirmed it belongs to the same game window.  A window over the
        game's own client area cannot be told apart from the game's own UI, so it is never blocked; the
        real guard is the **client-rectangle check** in ``_screen_point`` (a click must land inside the
        game's client area, never on its title bar or a border), and any window that is neither the
        game's main window nor ours is at least WARNED about, with the whole stack over that point.
        """

        if self.dry_run:
            return None
        if self._point_owner_fn is not None:
            # The injected reader is authoritative (tests, and any caller that already knows).
            return self._point_owner_fn(int(x), int(y))
        try:
            hwnd, title, class_name = window_at_point(x, y)
        except Exception:
            LOG.debug("auto reconnect: the window under the click point could not be read",
                      exc_info=True)
            return None
        if not hwnd:
            return None
        name = title or class_name or f"hwnd {hwnd}"
        # FIRST and unconditionally: never click our own window.  This needs no game handle, and it was
        # the hole through which a click reached the panel's 自动重连 button (the operator's report).
        owner_pid = window_process_id(hwnd)
        if owner_pid and owner_pid == os.getpid():
            LOG.error("auto reconnect: (%d, %d) is OUR OWN window %r (hwnd %s, pid %s) - the click is "
                      "refused; the panel must not be clicked", x, y, name, hwnd, owner_pid)
            return (hwnd, name)
        game = self._game_hwnd()
        if not game:
            # Without the game's window handle nothing can be compared (rule 1 already passed).
            LOG.warning("auto reconnect: the game window %r could not be resolved - the click point "
                        "(%d, %d) is used without an ownership check", self.window_title, x, y)
            return None
        if hwnd == game:
            return None
        game_pid = window_process_id(game)
        if owner_pid and game_pid and owner_pid == game_pid:
            LOG.info("auto reconnect: (%d, %d) belongs to the game's own process (hwnd %s) - accepted",
                     x, y, hwnd)
            return None
        owner_image = process_image_name(owner_pid)
        game_image = process_image_name(game_pid) if game_pid else ""
        if owner_image and game_image and owner_image == game_image:
            LOG.info("auto reconnect: (%d, %d) belongs to %r (hwnd %s) which runs the game's own "
                     "executable (%s) - accepted", x, y, name, hwnd, game_image)
            return None
        for hint in PAGE_LOGIN_WINDOW_HINTS:
            if hint.casefold() in name.casefold() or hint.casefold() in class_name.casefold():
                LOG.info("auto reconnect: (%d, %d) belongs to %r (hwnd %s) - the game's login UI by "
                         "name (%s) - accepted", x, y, name, hwnd, hint)
                return None
        if game:
            try:
                import win32gui

                game_class = win32gui.GetClassName(game)
                if game_class and class_name and game_class == class_name:
                    return None
            except Exception:
                LOG.debug("auto reconnect: the game window class could not be read", exc_info=True)
        if self.window_title and self.window_title.casefold() in name.casefold():
            return None

        # Anything else is a FOREIGN window: clicking it hands the foreground to that window and the
        # game never sees the click (measured: a maximised browser covered every point).
        stack = windows_over_point(x, y)
        topmost = any(item["hwnd"] == hwnd and item["topmost"] for item in stack)
        LOG.error("auto reconnect: (%d, %d) belongs to %r (hwnd %s, pid %s, %s, rect %s) - NOT the "
                  "game (hwnd %s, pid %s, %s) - the click is REFUSED%s.  Windows over that point: %s",
                  x, y, name, hwnd, owner_pid, owner_image or "?", window_rect(hwnd), game, game_pid,
                  game_image or "?", " (that window is TOPMOST)" if topmost else "",
                  " | ".join(f"「{item['title']}」 hwnd {item['hwnd']} pid {item['pid']}"
                             f"{' topmost' if item['topmost'] else ''}"
                             for item in stack) or "none listed")
        return (hwnd, name)

    def _report_click_failure(self, fallback: str) -> None:
        """Report why a click did not happen - the precise reason when there is one."""

        detail = self._click_failure or fallback
        self._click_failure = None
        self._report("failed", detail)

    def page_references(self) -> dict:
        """The three page signatures, loaded once."""

        if not self._page_references_loaded:
            self._page_references = load_page_references()
            self._page_references_loaded = True
            if not self._page_references:
                LOG.warning("auto reconnect: no page signature is available - the steps cannot be "
                            "verified by page")
        return self._page_references

    def reload_page_references(self) -> dict:
        """Read the page signatures again (after they were replaced)."""

        self._page_references_loaded = False
        self._page_references = {}
        return self.page_references()

    def page_now(self) -> tuple[Optional[str], float]:
        """Which page the game shows right now -> ``(page, score)``.

        ``None`` means "none of the three" - i.e. the game is past the login and the select pages,
        which is exactly what the end of the sequence must prove.
        """

        if self._page_fn is not None:
            return self._page_fn()
        frame = self._capture()[0]
        page, score = detect_page(frame, self.page_references())
        if page is not None:
            return (page, score)
        # The login page can also be recognised by its COLOUR (the shipped cream reference), which is
        # independent of the crop signatures: measured in the field, the page scored 0.51 on the crops
        # and left the login step unverifiable even though the login page was on screen.
        evidence = self.measure_login_page_colour()
        if evidence is not None and evidence.is_login_page():
            LOG.info("auto reconnect: no signature matched (%.2f), but the login page's COLOUR did "
                     "(%s) - the page is the login page", score, evidence.describe())
            return (PAGE_LOGIN, 0.5 + min(0.49, float(evidence.fraction)))
        return (None, score)

    def _page_label(self, page: Optional[str]) -> str:
        return PAGE_LABELS.get(page, str(page))

    def page_verification_available(self) -> bool:
        """Whether the page can be measured at all.

        Without a page signature (or an injected `page_fn`) the steps fall back to the old
        pixel-change checks - the verifier must never be the reason a working sequence stops.
        """

        if self._page_fn is not None:
            return True
        return bool(self.page_references())

    def _step_done(self, expected: Optional[str], *, what: str, changed: bool) -> bool:
        """Whether a step reached ``expected``.

        With a page signature the PAGE decides (that is the operator's rule: each page has a
        signature - the login board with 连接/结束游戏, the world list with 蓝蜗牛, the channel
        list).  Without one the old pixel-change answer is used instead, so a missing reference
        only costs the extra verification, never the run.
        """

        if not self.page_verification_available():
            LOG.warning("auto reconnect: no page signature available - falling back to the pixel "
                        "check for %s (changed=%s)", what, changed)
            return bool(changed)
        return self._page_is(expected, what=what)

    def _page_is(self, expected: Optional[str], *, what: str) -> bool:
        """Log and report which page a step produced; True when it is the expected one."""

        page, score = self.page_now()
        LOG.info("auto reconnect: after %s the page is %s (score %.2f, expected %s)", what,
                 self._page_label(page), score, self._page_label(expected))
        self._report("page", f"{what} → {self._page_label(page)} ({score:.2f})")
        return page == expected

    def _await_page(self, expected: Optional[str], *, what: str, timeout: float,
                    first_pause: Optional[float] = None) -> bool:
        """Wait for the page to become ``expected``, polling every ``PAGE_POLL_SECONDS``.

        The operator: *"the process is stepping too quick, the game isn't react that quick"*.  A
        single sample after a fixed pause is what produced the misleading "the step did nothing"
        reports, so the page is now watched until it changes - and a step only fails when the whole
        timeout passed without a change.  ``first_pause`` keeps the operator's own step pauses (5 s
        after the login Enter, 3 s per step) BEFORE the first poll, so the wait only ever grows.
        """

        attempts = max(1, int(round(max(0.0, float(timeout)) / PAGE_POLL_SECONDS)))
        last_page: Optional[str] = None
        last_score = 0.0
        for attempt in range(1, attempts + 1):
            pause = PAGE_POLL_SECONDS if attempt > 1 or not first_pause else float(first_pause)
            if not self._sleep_checked(pause):
                return False
            last_page, last_score = self.page_now()
            if last_page == expected:
                LOG.info("auto reconnect: %s reached %s after %.1f s (score %.2f)", what,
                         self._page_label(expected), attempt * PAGE_POLL_SECONDS, last_score)
                self._report("page", f"{what} → {self._page_label(expected)} ({last_score:.2f})")
                return True
        LOG.warning("auto reconnect: %s waited %.1f s and the page is still %s (%.2f)", what,
                    attempts * PAGE_POLL_SECONDS, self._page_label(last_page), last_score)
        self._report("page", f"{what} → 仍是{self._page_label(last_page)}"
                             f"（等待 {attempts * PAGE_POLL_SECONDS:.0f} 秒）")
        self._dump_page_evidence(what)
        return False

    def _dump_page_evidence(self, what: str) -> None:
        """Log every page signature's score and save the frame, so a failure is readable.

        A page measurement that lands under the threshold says nothing about WHY: the per-page scores
        show which signature lost, and the saved JPG can be classified offline.
        """

        try:
            frame, _rect = self._capture()
            if frame is None:
                return
            scores = page_scores(frame, self.page_references())
            LOG.error("auto reconnect: page evidence for %s: %s", what,
                      ", ".join(f"{self._page_label(page)}={value:.3f}"
                                for page, value in sorted(scores.items(),
                                                          key=lambda item: -item[1]))
                      or "no signature available")
            if self._capture_fn is not None:
                # An injected capture means a test (or a caller that owns the frames): the scores are
                # logged, but nothing is written into screenshots/.
                return
            path = self.save_diagnostic_capture(prefix=f"page_{what[:12]}".replace(" ", "_"))
            if path is not None:
                LOG.error("auto reconnect: the frame for %s was saved to %s", what, path)
        except Exception:
            LOG.debug("auto reconnect: the page evidence could not be collected", exc_info=True)

    def _verify_or_pixel(self, expected: Optional[str], *, what: str, before, timeout: float,
                         first_pause: Optional[float] = None) -> bool:
        """The page result when the page can be measured, else the pixel comparison of v0404."""

        if self.page_verification_available():
            return self._await_page(expected, what=what, timeout=timeout, first_pause=first_pause)
        if not self._sleep_checked(timeout):
            return False
        changed = screen_change(before, self._capture()[0]) >= SCREEN_CHANGE_MIN
        LOG.warning("auto reconnect: no page signature - %s is verified by the pixel change (%s)",
                    what, changed)
        return bool(changed)

    def _client_point_to_screen(self, frame, rect, client_x: int, client_y: int):
        """A client point (measured on the 1366x768 frame) -> screen pixels.

        The captured frame and the client rectangle must be the same size; when they are not (a DPI
        mismatch between the picture and the window APIs) the point is scaled into the client's space,
        which is the space the origin belongs to.
        """

        origin_x, origin_y = int(rect[0]), int(rect[1])
        if frame is None:
            return origin_x + int(client_x), origin_y + int(client_y)
        frame_w = max(1, int(frame.shape[1]))
        frame_h = max(1, int(frame.shape[0]))
        client = self._client_size_of(self._frame_hwnd or self._game_hwnd())
        if client is None:
            return origin_x + int(client_x), origin_y + int(client_y)
        client_w, client_h = client
        scale_x = client_w / float(frame_w)
        scale_y = client_h / float(frame_h)
        if abs(scale_x - 1.0) < 0.02 and abs(scale_y - 1.0) < 0.02:
            return origin_x + int(client_x), origin_y + int(client_y)
        LOG.warning("auto reconnect: the frame is %dx%d but the client is %dx%d - the client point "
                    "(%d, %d) is scaled by (%.3f, %.3f) before clicking", frame_w, frame_h, client_w,
                    client_h, client_x, client_y, scale_x, scale_y)
        return (origin_x + int(round(int(client_x) * scale_x)),
                origin_y + int(round(int(client_y) * scale_y)))

    def _client_size_of(self, hwnd: int):
        """The client size of a window -> (width, height), or None."""

        if not hwnd:
            return None
        try:
            import win32gui

            left, top, right, bottom = win32gui.GetClientRect(int(hwnd))
            width, height = int(right - left), int(bottom - top)
            if width > 0 and height > 0:
                return (width, height)
        except Exception:
            LOG.debug("auto reconnect: the client size of hwnd %s could not be read", hwnd,
                      exc_info=True)
        return None

    def _client_rect_on_screen(self):
        """The game's client area on screen -> (left, top, right, bottom), or None when unknown."""

        try:
            import win32gui

            game = self._game_hwnd()
            if not game:
                return None
            client_left, client_top, client_right, client_bottom = win32gui.GetClientRect(game)
            left, top = win32gui.ClientToScreen(game, (int(client_left), int(client_top)))
            right, bottom = win32gui.ClientToScreen(game, (int(client_right), int(client_bottom)))
            return (int(left), int(top), int(right), int(bottom))
        except Exception:
            LOG.debug("auto reconnect: the client rectangle could not be read", exc_info=True)
            return None

    def _take_keyboard(self) -> None:
        """Take the keyboard exclusively for this run, and drop any held modifier.

        Measured cause of the focus chaos the operator reported: Alt is this game's JUMP key, so the
        automation presses it regularly; an Alt that is still down turns a later Escape into
        **Alt+Esc** (switch window - "some times a folder steal its focus") and a later F4 into
        Alt+F4 (`shutdown_worker` sends that chord).  While the reconnect runs it therefore owns the
        keyboard: every other caller is refused, and Alt/Ctrl/Shift are released before the first key.
        """

        sender = self.key_sender
        for name in ("begin_exclusive",):
            hook = getattr(sender, name, None)
            if callable(hook):
                try:
                    hook("auto-reconnect")
                except Exception:
                    LOG.debug("auto reconnect: the keyboard ownership could not be taken",
                              exc_info=True)
        self._release_modifiers("run start")

    def _release_keyboard(self) -> None:
        sender = self.key_sender
        self._release_modifiers("run end")
        hook = getattr(sender, "end_exclusive", None)
        if callable(hook):
            try:
                hook("auto-reconnect")
            except Exception:
                LOG.debug("auto reconnect: the keyboard ownership could not be released",
                          exc_info=True)

    def _release_modifiers(self, where: str) -> None:
        """Force Alt/Ctrl/Shift up so our keys can never become a shell shortcut."""

        hook = getattr(self.key_sender, "release_modifiers", None)
        if not callable(hook):
            return
        try:
            hook(owner="auto-reconnect", reason=f"auto reconnect ({where})")
        except Exception:
            LOG.debug("auto reconnect: the modifiers could not be released", exc_info=True)

    def _stand_down_automation(self) -> bool:
        """Clear the automation switch for the duration of the run -> whether it was set.

        The attack/movement workers watch the same switch that the reconnect's own keyboard input
        needs, so arming input for the reconnect also starts them: the field log shows the attack
        worker pressing `a` every second while the reconnect was selecting a channel.
        """

        if self.reconnect_active_event is not None:
            try:
                self.reconnect_active_event.set()
                LOG.info("auto reconnect: the focus gate is told that the reconnect owns the machine")
            except Exception:
                LOG.debug("auto reconnect: the reconnect flag could not be raised", exc_info=True)
        event = self.automation_event
        if event is None:
            return False
        try:
            was_set = bool(event.is_set())
            if was_set:
                event.clear()
                LOG.info("auto reconnect: automation (patrol/attack) stood down for this run")
                self._report("input", "已暂停自动化（巡逻/攻击），本机接管输入")
            return was_set
        except Exception:
            LOG.debug("auto reconnect: the automation switch could not be cleared", exc_info=True)
            return False

    def _restore_automation(self, was_set: bool) -> None:
        """Put the automation switch back the way it was (and let the focus gate work again)."""

        if self.reconnect_active_event is not None:
            try:
                self.reconnect_active_event.clear()
                LOG.info("auto reconnect: the focus gate may re-arm the automation again")
            except Exception:
                LOG.debug("auto reconnect: the reconnect flag could not be lowered", exc_info=True)
        event = self.automation_event
        # Wait one focus-gate poll so the automation switch really is cleared before the flag drops:
        # otherwise the gate could re-arm it in the same moment the reconnect ends.
        self._sleep(0.35)
        if event is None or not was_set:
            return
        try:
            event.set()
            LOG.info("auto reconnect: automation restored after the run")
        except Exception:
            LOG.debug("auto reconnect: the automation switch could not be restored", exc_info=True)

    @property
    def press_enter_for_offline_prompt(self) -> bool:
        """Alias for older callers: the offline-prompt handling as a whole (click + Enter)."""

        return self.dismiss_offline_prompt

    @press_enter_for_offline_prompt.setter
    def press_enter_for_offline_prompt(self, value: bool) -> None:
        self.dismiss_offline_prompt = bool(value)

    def _game_is_active(self) -> bool:
        """Whether the GAME is the active application - its own dialogs included.

        ``igwUserLoginDialog`` (the game's login dialog) is a separate top-level window: when it has
        the foreground, the configured title/hwnd check fails even though the game is exactly as
        active as a click needs it to be.  A click is delivered to the window under the cursor, so
        "a window of the game's process has the foreground" is the right test for clicking; the
        keyboard path keeps using ``_window_is_foreground``, because keys go to the focused window.
        """

        try:
            import win32gui

            foreground = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            return self._window_is_foreground()
        if not foreground:
            return self._window_is_foreground()
        game = self._game_hwnd()
        if game and foreground == game:
            return True
        foreground_pid = window_process_id(foreground)
        game_pid = window_process_id(game) if game else 0
        if foreground_pid and game_pid and foreground_pid == game_pid:
            LOG.info("auto reconnect: the active window is another window of the GAME's process "
                     "(%s) - that counts as active for clicking", self._foreground_title())
            return True
        foreground_image = process_image_name(foreground_pid)
        game_image = process_image_name(game_pid) if game_pid else ""
        if foreground_image and game_image and foreground_image == game_image:
            LOG.info("auto reconnect: the active window is the game's own login client (%s, pid %s) "
                     "- that counts as active", foreground_image, foreground_pid)
            return True
        try:
            title = win32gui.GetWindowText(foreground) or ""
        except Exception:
            title = ""
        if self.window_title and self.window_title.casefold() in title.casefold():
            return True
        for hint in PAGE_LOGIN_WINDOW_HINTS:
            if hint.casefold() in title.casefold():
                return True
        return self._window_is_foreground()

    def _log_click_window(self, label: str) -> None:
        """Log the window every click is computed from - the picture and the point must agree.

        The operator's report was "the cursor clicks outside the game window", and with six processes
        under `Maplestory_Classic` the only way to check that is to print, right next to the click, the
        handle, class, rectangle and client origin the point was computed from.
        """

        hwnd = self._game_hwnd()
        if not hwnd:
            LOG.warning("auto reconnect: %s - no game window handle, the click is unverified", label)
            return
        try:
            import win32gui

            LOG.info("auto reconnect: %s uses hwnd %s class %s rect %s client origin %s (frame hwnd "
                     "%s)", label, hwnd, win32gui.GetClassName(hwnd),
                     tuple(int(v) for v in win32gui.GetWindowRect(hwnd)),
                     self._client_rect_on_screen(), self._frame_hwnd)
        except Exception:
            LOG.debug("auto reconnect: the click window could not be described", exc_info=True)

    def _focus_summary(self) -> str:
        """Who stole the foreground during the run (empty when nobody did)."""

        watch = self._focus_watch
        if watch is None:
            return ""
        try:
            return watch.summary()
        except Exception:
            return ""

    def _report_with_focus(self, detail: str) -> None:
        """Report a failure together with the focus history - the missing evidence."""

        summary = self._focus_summary()
        if summary and summary != "焦点没有被抢":
            detail = f"{detail}；{summary}"
        self._report("failed", detail)

    def _geometry_dump(self, client_x: int, client_y: int, screen_x: int, screen_y: int) -> str:
        """Everything about where a click point is, for the log (used when one is refused)."""

        try:
            import win32gui

            cursor = win32gui.GetCursorPos()
            game = self._game_hwnd()
            window = win32gui.GetWindowRect(game) if game else None
            client = win32gui.ClientToScreen(game, (0, 0)) if game else None
            return (f"client ({client_x}, {client_y}) -> screen ({screen_x}, {screen_y}); "
                    f"game hwnd {game} window_rect {window} client_origin {client}; "
                    f"cursor {cursor}; foreground {self._foreground_title()!r}")
        except Exception:
            LOG.debug("auto reconnect: the geometry dump failed", exc_info=True)
            return f"client ({client_x}, {client_y}) -> screen ({screen_x}, {screen_y})"

    def _lower_own_windows(self) -> int:
        """Push the ASSISTANT's own windows behind everything, without moving or activating them.

        The measured situation: the assistant's window sits over the game's click point, so a click
        there would activate the ASSISTANT instead of reaching the game ("the window keeps losing
        focus to assistant", "it will go to click the title of the game window").  The run cannot ask
        the operator to move his panel out of the way, so the panel is lowered for the click:
        ``SetWindowPos(HWND_BOTTOM)`` with SWP_NOACTIVATE keeps the window exactly where it is and
        only changes its z-order.  Returns how many windows were lowered.
        """

        if self.dry_run:
            return 0
        try:
            import win32con
            import win32gui
            import win32process

            ours = os.getpid()
            lowered = []

            def visit(hwnd, _param):
                if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                    return True
                try:
                    if win32process.GetWindowThreadProcessId(hwnd)[1] != ours:
                        return True
                except Exception:
                    return True
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if style & win32con.WS_EX_TOPMOST:
                    # a topmost own window (an overlay) is lowered as well: it covers the game
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                          win32con.SWP_NOACTIVATE)
                win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                                      win32con.SWP_NOACTIVATE)
                lowered.append(hwnd)
                return True

            win32gui.EnumWindows(visit, None)
            if lowered:
                LOG.info("auto reconnect: lowered %d assistant window(s) %s so the game is not "
                         "covered", len(lowered), lowered)
            return len(lowered)
        except Exception:
            LOG.debug("auto reconnect: the assistant windows could not be lowered", exc_info=True)
            return 0

    def _screen_point(self, client_x: int, client_y: int, label: str):
        """The screen point for a client point, only when the GAME owns that point.

        Returns ``(screen_x, screen_y)``, or ``None`` after reporting why no click was sent.  This
        is what the field report was about: clicking a point that belongs to another window hands
        the foreground to that window, so the game never sees the click and the assistant takes the
        focus back (the operator: "the window keeps losing focus to assistant, so the click is not
        successfully emitted to the game").
        """

        self._click_failure = None
        for attempt in (1, 2):
            frame, rect = self._capture()
            screen_x, screen_y = self._client_point_to_screen(frame, rect, client_x, client_y)
            owner = self._point_owner(screen_x, screen_y)
            if owner is None:
                client_rect = self._client_rect_on_screen()
                if client_rect is not None and not point_in_rect(screen_x, screen_y, client_rect):
                    # Inside the game WINDOW but not its client area: the title bar or a border.
                    # A click there activates - or even drags - the window instead of reaching the
                    # game, which is exactly the operator's report ("it will go to click the title
                    # of the game window").
                    LOG.error("auto reconnect: the point for %s is inside the game WINDOW but "
                              "outside its CLIENT area (client %s) - no click is sent.  %s", label,
                              client_rect, self._geometry_dump(client_x, client_y, screen_x,
                                                               screen_y))
                    self._click_failure = (f"点击位置 ({screen_x}, {screen_y}) 不在游戏客户区内"
                                           f"（客户区 {client_rect}）")
                    return None
                return screen_x, screen_y
            if attempt == 1:
                LOG.warning("auto reconnect: the point for %s at screen (%d, %d) belongs to %r - "
                            "lowering the assistant and bringing the game forward, then checking "
                            "again (%s)", label, screen_x, screen_y, owner[1],
                            self._geometry_dump(client_x, client_y, screen_x, screen_y))
                self._lower_own_windows()
                self._prepare_window()
                self._sleep(CLICK_RETRY_WAIT_SECONDS)
                continue
            LOG.error("auto reconnect: the point for %s still belongs to the ASSISTANT window %r "
                      "(hwnd %s) even after lowering it - NO click is sent, because a click there "
                      "would hand the foreground to the assistant.  %s", label, owner[1], owner[0],
                      self._geometry_dump(client_x, client_y, screen_x, screen_y))
            self._click_failure = (f"点击位置 ({screen_x}, {screen_y}) 属于助手窗口「{owner[1]}」"
                                   f"（请让游戏窗口保持在前台、不要被助手窗口挡住）")
            return None
        return None

    def _click(self, x: int, y: int, *, keep_focus: bool = False, settle: Optional[float] = None,
               hold: Optional[float] = None) -> bool:
        """One left click at a screen position (injected in tests, logged in --dry-run).

        ``keep_focus`` makes the game the active window in the last moment before the click, because
        a game ignores clicks while it is not active.  ``settle``/``hold`` override the shipped
        click timing (see :func:`click_screen`).
        """

        if self._click_fn is not None:
            return bool(self._click_fn(int(x), int(y)))
        if self.dry_run:
            LOG.info("auto reconnect: DRY-RUN click at (%d, %d)", x, y)
            return True
        return click_screen(x, y, keep_focus=self._game_hwnd() if keep_focus else 0,
                            settle=CLICK_SETTLE_SECONDS if settle is None else float(settle),
                            hold=CLICK_HOLD_SECONDS if hold is None else float(hold))

    def _login_board(self) -> tuple[int, int, int, int]:
        """``LOGIN_BOARD_CLIENT_BOX`` mapped from the preset client onto the client on screen."""

        return login_client_box(LOGIN_BOARD_CLIENT_BOX, self._click_size())

    def _click_size(self) -> tuple[int, int]:
        """The client size the constants are mapped into, WITHOUT capturing a frame.

        The size of the last captured frame is enough (and a capture inside a measurement loop would
        consume a frame the loop is waiting for): an injected ``_client_size_fn`` still wins, then the
        last frame, and only a run that has never captured falls back to a fresh capture.
        """

        if self._client_size_fn is not None:
            size = self._client_size_fn()
            if size:
                return (int(size[0]), int(size[1]))
        if self._last_frame_size is not None:
            return self._last_frame_size
        return self._client_size()

    def _in_exit_button(self, client_x: int, client_y: int) -> bool:
        """Whether a client point sits on 结束游戏 - the button that must never be clicked."""

        size = self._click_size()
        for box in LOGIN_EXIT_BUTTON_CLIENT_BOXES:
            if point_in_box(client_x, client_y, login_client_box(box, size)):
                return True
        return False

    def _login_board_box(self):
        """The login board's box in client pixels (the largest region of the page colour)."""

        reference = self._login_reference()
        frame, _rect = self._capture()
        if reference is None or frame is None:
            return None, frame
        evidence = analyse_login_page(frame, reference)
        if evidence is None:
            return None, frame
        return evidence.bbox, frame

    def _click_connect(self) -> bool:
        """Click 连接 - the only point the sequence ever clicks on the login page.

        ``LOGIN_CONNECT_CLICK_CLIENT`` (a measured client point) wins when it is set; otherwise
        OpenCV locates the button on the board.  Returns False when there is nothing to click,
        which is not an error: the caller falls through to Enter.  The board itself is **never**
        clicked blindly - v0394 did that with a fixed fraction and hit 结束游戏 in the field.
        """

        if LOGIN_CONNECT_CLICK_CLIENT is not None:
            board_box = self._login_board()
            client_x, client_y = login_client_point(LOGIN_CONNECT_CLICK_CLIENT, self._click_size())
            if not point_in_box(client_x, client_y, board_box):
                # A point outside the board is exactly the v0394 failure mode (结束游戏 lives
                # outside it), so it is refused instead of clicked.
                LOG.error("auto reconnect: the configured 连接 point (%d, %d) is outside the "
                          "login board %s - no click is sent", client_x, client_y, board_box)
                self._report("failed", f"「连接」点 ({client_x}, {client_y}) 不在登录面板范围内，已取消点击")
                return False
            if self._in_exit_button(client_x, client_y):
                LOG.error("auto reconnect: the configured 连接 point (%d, %d) is inside the "
                          "结束游戏 button - no click is sent", client_x, client_y)
                self._report("failed", f"「连接」点 ({client_x}, {client_y}) 落在结束游戏按钮上，已取消点击")
                return False
            LOG.info("auto reconnect: 连接 uses the measured client point (%d, %d) inside the "
                     "board %s", client_x, client_y, board_box)
        else:
            box, frame = self._login_board_box()
            if frame is None:
                LOG.warning("auto reconnect: no frame - 连接 cannot be located")
                return False
            template = self._connect_template()
            button = find_connect_button(frame, box, template=template)
            if button is None:
                LOG.warning("auto reconnect: 连接 not located on the board - no click is sent "
                            "(the login falls back to Enter)")
                return False
            left, top, width, height = button
            client_x = int(round(left + width / 2.0))
            client_y = int(round(top + height / 2.0))
            LOG.info("auto reconnect: 连接 located at client box=(%d, %d, %d, %d)",
                     left, top, width, height)
        point = self._screen_point(client_x, client_y, "「连接」")
        if point is None:
            self._report_click_failure("点击「连接」失败")
            return False
        screen_x, screen_y = point
        LOG.info("auto reconnect: clicking 连接 at client (%d, %d) = screen (%d, %d), foreground "
                 "%r", client_x, client_y, screen_x, screen_y, self._foreground_title())
        self._report("connect", f"已点击「连接」 ({screen_x}, {screen_y})")
        if not self._click(screen_x, screen_y):
            LOG.error("auto reconnect: the 连接 click failed")
            self._report_click_failure("点击「连接」失败")
            return False
        return self._sleep_checked(ACTIVATE_CLICK_WAIT_SECONDS)

    def _connect_template(self):
        """The shipped 连接 button crop, when a measurement provided one."""

        if self._connect_template_loaded:
            return self._connect_template_image
        self._connect_template_loaded = True
        import cv2

        for folder in (SCREENSHOTS_DIR, ASSETS_DIR):
            path = folder / CONNECT_BUTTON_REFERENCE_NAME
            if path.is_file():
                image = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if image is not None and image.size:
                    LOG.info("auto reconnect: 连接 template loaded from %s (%dx%d)",
                             path, image.shape[1], image.shape[0])
                    self._connect_template_image = image
                    return image
        return None

    def _activate_window(self) -> bool:
        """Focus the game window, then click inside it so it accepts keyboard input.

        The order is the operator's: focus -> click -> Enter.  The window can be foreground and
        still ignore keys until the game itself has seen a click (measured in the field with
        v0391/v0392: `key hold complete=enter` was logged while the login page did nothing), so
        the sequence always clicks a harmless spot first - ``ACTIVATE_CLICK_CLIENT`` inside the
        client, far from every button and list entry.
        """

        if not self._window_is_foreground():
            if not self._prepare_window():
                return False
            # Give Windows a moment to really hand the foreground over: the operator starts the
            # manual test from the assistant window, so the game is not instantaneously in front
            # ("is that you start too quick?").
            if not self._sleep_checked(CLICK_RETRY_WAIT_SECONDS):
                return False
        frame, rect = self._capture()
        if frame is None:
            LOG.error("auto reconnect: no game window frame - cannot click to activate it")
            self._report("failed", "无法截取游戏窗口（无法点击激活）")
            return False
        LOG.info("auto reconnect: the frame for the activation click is %dx%d from client origin "
                 "(%d, %d)", int(frame.shape[1]), int(frame.shape[0]), int(rect[0]), int(rect[1]))
        client_x, client_y = ACTIVATE_CLICK_CLIENT
        self._log_click_window("激活")
        point = self._screen_point(client_x, client_y, "游戏窗口（激活）")
        if point is None:
            self._report_click_failure("点击游戏窗口失败（无法激活输入）")
            return False
        screen_x, screen_y = point
        LOG.info("auto reconnect: activation click at client (%d, %d) = screen (%d, %d); client "
                 "origin (%d, %d), foreground %r", client_x, client_y, screen_x, screen_y,
                 int(rect[0]), int(rect[1]), self._foreground_title())
        self._report("activate", f"已点击游戏窗口 ({screen_x}, {screen_y}) 激活输入")
        if not self._click(screen_x, screen_y, keep_focus=True):
            LOG.error("auto reconnect: the activation click failed")
            self._report_click_failure("点击游戏窗口失败（无法激活输入）")
            return False
        if not self._sleep_checked(ACTIVATE_CLICK_WAIT_SECONDS):
            return False
        if not self._game_is_active():
            # The activation click did nothing because the game is not active: without this the rest
            # of the sequence would type and click into a window that is not listening.
            LOG.warning("auto reconnect: the game is still NOT the active window after the "
                        "activation click (foreground %r) - bringing it forward again",
                        self._foreground_title())
            if not self._prepare_window():
                self._report("failed", "游戏窗口没有激活（激活点击没有生效）")
                return False
            if not self._sleep_checked(CLICK_RETRY_WAIT_SECONDS):
                return False
        return True

    def _enter_until_page(self, expected: Optional[str], *, what: str,
                          wait: Optional[float] = None,
                          attempts: int = PAGE_STEP_ATTEMPTS) -> bool:
        """Press Enter until the page becomes ``expected`` (the game confirms with Enter).

        This is the per-step verifier the operator asked for: the key is not "sent and assumed", the
        page is watched afterwards for ``wait`` seconds, and Enter is pressed again while the page
        has not changed.  ``expected=None`` means "no longer one of the three pages", i.e. in game.
        """

        pause = PAGE_WAIT_WORLD_SECONDS if wait is None else float(wait)
        label = self._page_label(expected)
        for attempt in range(1, max(1, int(attempts)) + 1):
            before = self._capture()[0]
            reason = self._press("enter")
            if reason:
                self._report("failed", reason)
                return False
            if self._verify_or_pixel(expected, what=f"{what} Enter ({attempt})", before=before,
                                     timeout=pause, first_pause=SELECT_WINDOW_WAIT_SECONDS):
                return True
            LOG.warning("auto reconnect: after the Enter for %s the page is not %s yet (%d/%d)",
                        what, label, attempt, attempts)
        return False

    def _dismiss_offline_prompt(self) -> bool:
        """Close the expected 掉线提示窗口 before reconnecting.

        The missing-marker watcher has already confirmed the login-page state.
        The supplied prompt-paper square is not reliable enough to decide
        whether a disconnect happened, nor to decide whether this fixed
        confirmation click is needed.  Use the known button position, then
        verify that a login-family page is available before continuing.
        """

        if not self.dismiss_offline_prompt:
            return True
        attempts = max(1, OFFLINE_PROMPT_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            point = offline_prompt_close_client_point(self._click_size())
            LOG.info(
                "auto reconnect: clicking expected 掉线提示「确定」at client (%d, %d) "
                "(%d/%d)", point[0], point[1], attempt, attempts,
            )
            self._report("prompt", f"第 {attempt} 次点击掉线提示「确定」"
                         f"（{point[0]}, {point[1]}）")
            if not self._click_client(point[0], point[1], "prompt", "掉线提示「确定」按钮"):
                self._report("failed", self._click_failure or "掉线提示「确定」按钮未点击成功")
                return False
            if not self._sleep_checked(OFFLINE_PROMPT_WAIT_SECONDS):
                return False
            if self._prompt_is_gone(attempt, attempts, "点击「确定」"):
                LOG.info("auto reconnect: 掉线提示 closed after click %d/%d", attempt, attempts)
                self._report("prompt", "掉线提示已关闭")
                return True

        LOG.error("auto reconnect: detected 掉线提示 did not close after %d click(s)", attempts)
        self._report("failed", f"掉线提示窗口没有关闭（已点击 {attempts} 次）；本次重连停止")
        return False

    def _login_page_already_showing(self) -> bool:
        """Whether the login page (its board) is visible, i.e. no prompt is covering it."""

        if not self.page_verification_available():
            return False
        try:
            page, _score = self.page_now()
        except Exception:
            return False
        return page == PAGE_LOGIN

    def _prompt_is_gone(self, attempt: int, attempts: int, what: str) -> bool:
        """Whether the login page (or a later page) is showing, i.e. the prompt really closed."""

        if not self.page_verification_available():
            # Without the page signatures there is nothing to verify the prompt with, so the shipped
            # "one action and carry on" behaviour is kept.
            return True
        page, score = self.page_now()
        if page in (PAGE_LOGIN, PAGE_WORLD, PAGE_CHANNEL):
            LOG.info("auto reconnect: the 掉线提示 window is closed after %s (%d/%d) - the page is "
                     "%s (%.2f)", what, attempt, attempts, self._page_label(page), score)
            self._report("prompt", f"掉线提示已关闭（现在是{self._page_label(page)} {score:.2f}）")
            return True
        LOG.warning("auto reconnect: after %s (%d/%d) the page is %s (%.2f) - the 掉线提示 window "
                    "still looks open (the login board is covered)", what, attempt, attempts,
                    self._page_label(page), score)
        # ... but only keep trying while the LOGIN family is on screen at all: the prompt page still
        # shows the login-page colour (9.2 % measured), so a frame without that colour is some other
        # page (in game) and further clicks/keys would hit the game.
        frame, _rect = self._capture()
        reference = self._login_reference()
        evidence = analyse_login_page(frame, reference) if reference is not None else None
        if evidence is None or evidence.fraction < LOGIN_PAGE_MIN_FRACTION:
            LOG.warning("auto reconnect: the page is not recognised and the login-page colour is not "
                        "there either (%s) - no further action for the prompt",
                        "no reference" if evidence is None else f"{evidence.fraction:.4f}")
            return True
        return False

    def _enter_game(self) -> bool:
        """Leave the login page and PROVE the world page appeared.

        The operator's rule (v0405): *"you didn't make a verifier for each step, this time the first
        step is failed but you didn't notice and retry"*.  The verifier is the page classifier - the
        login page is the one with the deep brown board (连接 and 结束游戏), the next page carries the
        world list (蓝蜗牛 ...).  The whole step (board click -> 连接 click -> Enter) is repeated up
        to ``PAGE_STEP_ATTEMPTS`` times until the world page is really on screen, and each action is
        checked on its own, so nothing is "sent and assumed".
        """

        for attempt in range(1, PAGE_STEP_ATTEMPTS + 1):
            page, score = self.page_now()
            if page in (PAGE_WORLD, PAGE_CHANNEL):
                # Already past the login: the board/连接/Enter clicks are not needed at all.
                self._report("login-done", f"已在{self._page_label(page)}（{score:.2f}）")
                return True
            LOG.info("auto reconnect: login step attempt %d/%d - the page is %s (%.2f)",
                     attempt, PAGE_STEP_ATTEMPTS, self._page_label(page), score)

            if LOGIN_BOARD_CLICK_CLIENT is not None:
                board_before = self._capture()[0]
                board_box = self._login_board()
                board_x, board_y = login_client_point(LOGIN_BOARD_CLICK_CLIENT, self._click_size())
                if not point_in_box(board_x, board_y, board_box):
                    LOG.error("auto reconnect: the board point %s is outside the board %s",
                              LOGIN_BOARD_CLICK_CLIENT, LOGIN_BOARD_CLIENT_BOX)
                    self._report("failed",
                                 f"登录面板点 {LOGIN_BOARD_CLICK_CLIENT} 不在面板范围内")
                    return False
                # The board click only gives the game a click; 连接 is the click that matters, so
                # a board click that cannot be sent must not end the run (the operator: "click login
                # board step always failed" - and then nothing else happened).
                if self._in_exit_button(board_x, board_y):
                    LOG.warning("auto reconnect: the board point (%d, %d) is on 结束游戏 - not "
                                "clicked", board_x, board_y)
                    self._report("board", "登录面板点落在结束游戏上，跳过该点击")
                elif not self._click_client(board_x, board_y, "board", "登录面板"):
                    LOG.warning("auto reconnect: the login board click could not be sent (%s) - "
                                "continuing with 连接", self._click_failure)
                    self._click_failure = None
                    self._report("board", "登录面板没能点击，继续点「连接」")
                else:
                    if not self._sleep_checked(ACTIVATE_CLICK_WAIT_SECONDS):
                        return False
                    # The board click is a "give the game a click" action, so ONLY the page may
                    # confirm it: a pixel change here means nothing (it is what made v0405 report
                    # the login step as done while the login page was still on screen).
                    #
                    # This check is SOFT: a board click that changed nothing is the normal case (the
                    # login page stays until 连接 is clicked), so it must not go through
                    # ``_await_page`` - that dumps page evidence at ERROR level and saves a full
                    # diagnostic screenshot, which is what the operator read as "failed again" even
                    # though the very next click worked:
                    #   "page evidence for 点击登录面板: 世界选择页=0.368, 登录页=0.031"  (ERROR)
                    #   "the frame for 点击登录面板 was saved to ..."                 (ERROR)
                    if self.page_verification_available():
                        page, score = self.page_now()
                        if page in (PAGE_WORLD, PAGE_CHANNEL):
                            LOG.info("auto reconnect: the board click alone was enough - the page is "
                                     "%s (%.2f)", self._page_label(page), score)
                            self._report("login-done", "点击登录面板后已进入世界选择页")
                            return True
                        LOG.info("auto reconnect: the board click did not change the page (%s, %.2f) "
                                 "- that is expected; 连接 is the click that matters",
                                 self._page_label(page), score)
                    # without a page signature there is nothing to verify here: the board click is
                    # only "give the game a click", and the 连接 click below is what matters

            before_connect = self._capture()[0]
            if self._click_connect():
                if self._verify_or_pixel(PAGE_WORLD, what="点击「连接」", before=before_connect,
                                         timeout=PAGE_WAIT_SECONDS,
                                         first_pause=LOGIN_WAIT_SECONDS):
                    self._report("login-done", "「连接」已生效")
                    return True
                LOG.warning("auto reconnect: the 连接 click did not produce the world page")

            # ONE Enter per step attempt: the OUTER loop supplies the retries, so a stuck page
            # cannot turn into nine key presses.  The page decides, so a login page that reacted to
            # Enter counts even when the frame comparison is fooled.
            if self.page_verification_available():
                reason = self._press("enter", hold=LOGIN_ENTER_HOLD_SECONDS)
                if reason:
                    self._report("failed", reason)
                    return False
                if self._await_page(PAGE_WORLD, what="登录页 Enter", timeout=PAGE_WAIT_SECONDS,
                                    first_pause=LOGIN_WAIT_SECONDS):
                    self._report("login-done", "登录页 Enter 已生效")
                    return True
            elif self._leave_login_page(report_failure=False, attempts=1):
                self._report("login-done", "登录页 Enter 已生效（画面变化）")
                return True
            page, score = self.page_now()
            LOG.warning("auto reconnect: login step attempt %d/%d did not reach the world page "
                        "(still %s, %.2f)", attempt, PAGE_STEP_ATTEMPTS,
                        self._page_label(page), score)

        page, score = self.page_now()
        LOG.error("auto reconnect: the login step failed %d times - the page is still %s (%.2f); "
                  "%s", PAGE_STEP_ATTEMPTS, self._page_label(page), score, self._focus_summary())
        self._report_with_focus(
            f"登录步骤失败 {PAGE_STEP_ATTEMPTS} 次，页面还是{self._page_label(page)}"
            f"（{score:.2f}）")
        return False

    def _leave_login_page(self, *, report_failure: bool = True,
                          attempts: int = LOGIN_ENTER_ATTEMPTS) -> bool:
        """Press Enter on the login page until the game ACTUALLY reacts.

        The page is not assumed to be gone: the frame before and after each Enter is compared.
        That is the measured field failure (v0391, 22:35) - the key was reported delivered, the
        login page stayed, and the rest of the sequence then typed into nothing.

        Why not simply re-measure the page colour: the next screen the game shows (the world
        list) is cream-themed as well, so "is the page colour still there" cannot tell the login
        page from the select window - a pixel-change test can, and it needs no reference image.
        The colour evidence is still logged, because it is what the gate is tuned with.
        """

        before = self._capture()[0]
        total = max(1, int(attempts))
        for attempt in range(1, total + 1):
            self._report("login-page", f"pressing Enter ({attempt}/{total})")
            reason = self._press("enter", hold=LOGIN_ENTER_HOLD_SECONDS)
            if reason:
                self._report("failed", reason)
                return False
            if not self._sleep_checked(LOGIN_WAIT_SECONDS):
                return False
            after = self._capture()[0]
            if after is None:
                LOG.warning("auto reconnect: no frame after Enter - continuing without "
                            "verification")
                return True
            change = screen_change(before, after)
            evidence = self.measure_login_page_colour()
            LOG.info("auto reconnect: after Enter attempt %d/%d the screen changed by %.2f "
                     "(login-page colour %s)", attempt, LOGIN_ENTER_ATTEMPTS, change,
                     evidence.describe() if evidence is not None else "n/a")
            if change >= SCREEN_CHANGE_MIN:
                LOG.info("auto reconnect: the login page reacted to Enter (change %.2f)",
                         change)
                return True
            LOG.warning("auto reconnect: the screen did not change after Enter attempt %d/%d "
                        "(%.2f < %.2f); pressing again", attempt, total, change, SCREEN_CHANGE_MIN)
            before = after
        LOG.error("auto reconnect: the login page did not react to %d Enter press(es)", total)
        if report_failure:
            # The caller may be a retry loop that verifies by page instead, and reports once at the
            # end - a premature "failed" line would mark a run that recovered.
            self._report("failed", f"登录页没有反应（Enter 按了 {total} 次仍未生效）")
        return False

    def save_diagnostic_capture(
        self, prefix: str = "select_window", folder: Optional[Path] = None
    ) -> Optional[Path]:
        """Save one frame of the game window for diagnosis.

        The sequence itself needs no geometry any more (it is keyboard only), so this is only a
        diagnostic dump: a frame of whatever the game shows right now, written as JPG.  Returns
        the written path, or None when the window could not be captured.
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
        matched = evidence.is_login_page()
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
        """Bring the game window forward so keys reach it.

        Returns False with the reason reported, so "game window is not available" never hides
        which of the two steps failed.
        """

        sender = self.key_sender
        if sender is None:
            self._report("failed", "没有按键发送器")
            return False
        attempt = 0
        while not (self.stop_event.is_set() or self._cancel_requested.is_set()):
            attempt += 1
            self._report("focus", f"正在重新定位游戏窗口（第 {attempt} 次）")
            try:
                # select_window first tries the last known HWND and then searches
                # the desktop for a replacement if that window can no longer be
                # focused.  This is shared by every automation workflow.
                selected = sender.select_window()
            except Exception:
                LOG.warning("auto reconnect: game-window re-anchor attempt %d failed",
                            attempt, exc_info=True)
                selected = False
            if selected is not False and self._window_is_foreground():
                if attempt > 1:
                    self._report("focus", "已重新定位并激活游戏窗口")
                return True

            detail = (f"无法激活游戏窗口；{FOCUS_REANCHOR_INTERVAL_SECONDS:.0f} 秒后"
                      f"重新查找游戏实例（第 {attempt} 次）")
            LOG.warning("auto reconnect: %s", detail)
            self._report("focus-wait", detail)
            if not self._sleep_checked(FOCUS_REANCHOR_INTERVAL_SECONDS):
                return False
        return False

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

    def login_page_visible_in_frame(self, image: Any) -> bool:
        """Confirm the login page from an already-captured game frame.

        This is the cheap second condition for the disconnect watch.  It does
        not focus the game, click, or start reconnecting; it only prevents a
        minimap-less in-game zone from being treated as an offline page.
        """

        if image is None:
            return False
        reference = self._login_reference()
        if reference is None:
            LOG.warning("auto reconnect: login reference unavailable for disconnect confirmation")
            return False
        try:
            import numpy as np

            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
            frame = rgb[:, :, ::-1].copy()
            evidence = find_login_page(frame, reference)
            if evidence is not None:
                LOG.info(
                    "auto reconnect: disconnect confirmation matched login page (%s)",
                    evidence.describe(),
                )
                return True
            return False
        except Exception:
            LOG.warning("auto reconnect: disconnect login-page confirmation failed", exc_info=True)
            return False

    def _select_world(self, world: str) -> bool:
        """Click the chosen world row and PROVE the channel page appears.

        The operator's model of the sequence: page 2 shows the world list (蓝蜗牛 ...), page 3 shows
        the channels.  A click selects a world; **Enter** confirms it and opens the channels, so both
        are tried and each is verified by the page - a click that the game ignored is noticed instead
        of being assumed (the earlier field failure logged in to 蓝蜗牛 while 蘑菇仔 had been chosen).
        """

        index = WORLD_NAMES.index(world)
        size = self._click_size()
        row_x, row_y = window_client_point(WORLD_ROW_CLIENT, size)
        step_x, step_y = window_client_delta(WORLD_ROW_STEP_CLIENT, size)
        select_window = window_client_box(SELECT_WINDOW_CLIENT_BOX, size)
        world_list_box = window_client_box(WORLD_LIST_CLIENT_BOX, size)
        client_x = row_x + index * step_x
        client_y = row_y + index * step_y

        page, score = self.page_now()
        if page == PAGE_CHANNEL:
            self._report("world", f"已在{self._page_label(page)}（{score:.2f}），不再改世界")
            LOG.info("auto reconnect: already on the channel page - the world step is skipped")
            return True
        if not point_in_box(client_x, client_y, select_window):
            LOG.error("auto reconnect: world point (%d, %d) is outside the select window %s",
                      client_x, client_y, select_window)
            self._report("failed", f"世界行坐标 ({client_x}, {client_y}) 不在选择窗口范围内")
            return False

        for attempt in range(1, PAGE_STEP_ATTEMPTS + 1):
            LOG.info("auto reconnect: world step attempt %d/%d for %s (row %d)", attempt,
                     PAGE_STEP_ATTEMPTS, world, index + 1)
            world_before = self._capture()[0]
            clicked = self._click_client(client_x, client_y, "world", f"{world} (row {index + 1})")
            if clicked:
                if not self._sleep_checked(SELECT_WINDOW_WAIT_SECONDS):
                    return False
                after = self._capture()[0]
                change = max(region_change(world_before, after, world_list_box),
                             screen_change(world_before, after))
                LOG.info("auto reconnect: after the %s click the world row changed by %.2f "
                         "(needs %.2f)", world, change, WORLD_LIST_CHANGE_MIN)
                if change >= WORLD_LIST_CHANGE_MIN:
                    # The selection really moved; now Enter may confirm it.
                    if self._verify_or_pixel(PAGE_CHANNEL, what=f"点击 {world} 世界行",
                                             before=world_before, timeout=PAGE_WAIT_WORLD_SECONDS,
                                             first_pause=SELECT_WINDOW_WAIT_SECONDS):
                        self._report("world", f"{world} (row {index + 1}) 已选择")
                        return True
                    if self._enter_until_page(PAGE_CHANNEL, what=f"{world} 世界"):
                        self._report("world", f"{world} (row {index + 1}) 已确认 (Enter)")
                        return True
                    LOG.warning("auto reconnect: the %s click and Enter did not open the channel "
                                "page", world)
                else:
                    # THE measured failure: a click that did not move the highlight, followed by an
                    # Enter that confirmed the default world (蓝蜗牛 instead of 蘑菇仔).
                    LOG.warning("auto reconnect: the click on %s (row %d) did NOT move the world "
                                "selection (%.2f < %.2f) - no Enter is sent, retrying the click",
                                world, index + 1, change, WORLD_LIST_CHANGE_MIN)
                    self._report("world", f"点击 {world} 世界行没有改变选择，重试")
            else:
                LOG.warning("auto reconnect: the click on world %s could not be sent", world)

            # Fallback: the list is horizontal, so walk right from the current selection - again only
            # confirming when the highlight actually moves.
            moved = False
            for _ in range(index):
                before_key = self._capture()[0]
                reason = self._press("right")
                if reason:
                    self._report("failed", reason)
                    return False
                if not self._sleep_checked(KEY_DELAY_SECONDS):
                    return False
                if region_change(before_key, self._capture()[0], world_list_box) \
                        >= WORLD_LIST_CHANGE_MIN:
                    moved = True
            if index and not moved:
                LOG.warning("auto reconnect: the right-arrow walk did not move the world selection "
                            "either - the world cannot be chosen reliably")
                continue
            if self._enter_until_page(PAGE_CHANNEL, what=f"{world} 世界（键盘）"):
                self._report("world", f"{world} (row {index + 1}, keyboard)")
                return True

        page, score = self.page_now()
        LOG.error("auto reconnect: the world step failed %d times - the page is %s (%.2f); %s",
                  PAGE_STEP_ATTEMPTS, self._page_label(page), score, self._focus_summary())
        self._report_with_focus(
            f"选择世界 {world} 失败 {PAGE_STEP_ATTEMPTS} 次（点击没有改变世界选择，未按 Enter，"
            f"否则会确认默认世界）；页面还是{self._page_label(page)}（{score:.2f}）")
        return False

    def _click_client(self, client_x: int, client_y: int, state: str, label: str) -> bool:
        """Click a point given in *client* pixels, retrying after a re-focus.

        A click can be lost when the game window is not really the foreground any more (the second
        launch of the drill reported exactly that), so the window is brought forward again and the
        click repeated up to ``CLICK_ATTEMPTS`` times; every attempt is logged.
        """

        # The focus is checked BEFORE the first click: measured in the field, the login-board
        # click needed attempt 2 because the window was not foreground yet.
        if not self._game_is_active():
            LOG.info("auto reconnect: focusing the game window before clicking %s", label)
            self._prepare_window()
            self._sleep(CLICK_RETRY_WAIT_SECONDS)
        for attempt in range(1, CLICK_ATTEMPTS + 1):
            if not self._game_is_active():
                LOG.warning("auto reconnect: the game is not the active window before clicking %s "
                            "(attempt %d/%d); bringing it back", label, attempt, CLICK_ATTEMPTS)
                self._prepare_window()
                self._sleep(CLICK_RETRY_WAIT_SECONDS)
            point = self._screen_point(client_x, client_y, label)
            if point is None:
                return False
            screen_x, screen_y = point
            LOG.info("auto reconnect: clicking %s at client (%d, %d) = screen (%d, %d) "
                     "(attempt %d/%d, foreground %r)", label, client_x, client_y, screen_x,
                     screen_y, attempt, CLICK_ATTEMPTS, self._foreground_title())
            self._report(state, f"已点击 {label} ({screen_x}, {screen_y})")
            if self._click(screen_x, screen_y, keep_focus=True):
                self._click_failure = None
                if self._game_is_active():
                    return True
                # A game ignores mouse clicks while it is not the ACTIVE window, so a click that
                # lost the focus did not count: take the focus back and click again instead of
                # accepting a click the game never saw.
                LOG.warning("auto reconnect: the click on %s lost the foreground (foreground is "
                            "%r) - taking it back and clicking again", label,
                            self._foreground_title())
                self._prepare_window()
                self._sleep(CLICK_RETRY_WAIT_SECONDS)
                continue
            LOG.warning("auto reconnect: the click on %s failed (attempt %d/%d)",
                        label, attempt, CLICK_ATTEMPTS)
            self._click_failure = f"点击 {label} ({screen_x}, {screen_y}) 没有送出 {CLICK_ATTEMPTS} 次"
            self._sleep(CLICK_RETRY_WAIT_SECONDS)
        LOG.error("auto reconnect: the click on %s failed %d times", label, CLICK_ATTEMPTS)
        return False

    def _double_click_client(self, client_x: int, client_y: int, state: str, label: str,
                             gap: float = DOUBLE_CLICK_GAP_SECONDS) -> bool:
        """Double-click a client point like a human hand does (the operator's channel rule).

        A single click only highlights the cell; the double click is what enters the channel, and his
        own mouse double click always works - so the gesture is made to look like his:

        * each click is HELD for ``DOUBLE_CLICK_HOLD_SECONDS`` (a human holds ~50-100 ms; the shipped
          30 ms may be too short for a game that samples the button),
        * the two clicks are ``gap`` seconds apart (the shipped 0.12 s may simply be outside the game's
          own double-click window - "i think maybe you click too quick or too slow"),
        * the second click does NOT re-settle the cursor (it is already on the pixel), so the interval
          is the gap it says it is instead of gap + jitter,
        * the real duration of the whole gesture is logged, so the next field log shows what the game
          was actually given.

        BOTH clicks also take the foreground first: a click that lands while the game is not the ACTIVE
        window is ignored, and losing only the SECOND click looks exactly like a failed double click
        (the first click still highlights the cell).
        """

        if not self._game_is_active():
            LOG.info("auto reconnect: focusing the game window before double-clicking %s", label)
            self._prepare_window()
            self._sleep(CLICK_RETRY_WAIT_SECONDS)
        point = self._screen_point(client_x, client_y, label)
        if point is None:
            return False
        screen_x, screen_y = point
        LOG.info("auto reconnect: double-clicking %s at client (%d, %d) = screen (%d, %d), gap %.2f s, "
                 "hold %.2f s, foreground %r", label, client_x, client_y, screen_x, screen_y, gap,
                 DOUBLE_CLICK_HOLD_SECONDS, self._foreground_title())
        self._report(state, f"已双击 {label} ({screen_x}, {screen_y})")
        started = time.perf_counter()
        if not self._click(screen_x, screen_y, keep_focus=True,
                           hold=DOUBLE_CLICK_HOLD_SECONDS):
            LOG.error("auto reconnect: the first click of the double-click on %s failed", label)
            return False
        self._sleep(gap)
        if not self._game_is_active():
            LOG.warning("auto reconnect: the game lost the foreground between the two clicks of the "
                        "double-click on %s (foreground is %r) - taking it back for the second click",
                        label, self._foreground_title())
            self._prepare_window()
        if not self._click(screen_x, screen_y, keep_focus=True, settle=0.0,
                           hold=DOUBLE_CLICK_HOLD_SECONDS):
            LOG.error("auto reconnect: the second click of the double-click on %s failed", label)
            return False
        LOG.info("auto reconnect: the double-click on %s took %.0f ms (gap %.2f s)",
                 label, (time.perf_counter() - started) * 1000.0, gap)
        return self._sleep_checked(CLICK_RETRY_WAIT_SECONDS)

    def _wait_for_list_change(self, before, what: str):
        """Wait for the channel list to react to an input.

        Returns ``(moved, frame, best_change)``.  The game repaints a scrolled list over several
        frames, so a single capture right after the wheel can still show the old row - which is
        exactly what made the shipped build report "not scrolled" for a scroll that had happened.
        """

        best = 0.0
        current = before
        for attempt in range(1, CHANNEL_LIST_SETTLE_ATTEMPTS + 1):
            pause = CHANNEL_SCROLL_WAIT_SECONDS if attempt == 1 else CHANNEL_LIST_POLL_SECONDS
            if not self._sleep_checked(pause):
                return False, current, best
            current = self._capture()[0]
            change = region_change(before, current,
                                   window_client_box(CHANNEL_LIST_CLIENT_BOX, self._click_size()))
            best = max(best, change)
            if change >= CHANNEL_LIST_CHANGE_MIN:
                LOG.info("auto reconnect: %s changed the channel list by %.2f (check %d)",
                         what, change, attempt)
                return True, current, best
        LOG.warning("auto reconnect: %s left the channel list unchanged (best %.2f < %.2f after "
                    "%d checks)", what, best, CHANNEL_LIST_CHANGE_MIN, CHANNEL_LIST_SETTLE_ATTEMPTS)
        return False, current, best

    def _next_wheel_method(self) -> Optional[str]:
        """The next untried wheel method, or None when all of them have been tried."""

        try:
            index = list(WHEEL_METHODS).index(self._wheel_method)
        except ValueError:
            index = -1
        for candidate in WHEEL_METHODS[index + 1:]:
            return candidate
        return None

    def _scroll_list_rows(self, rows: int) -> int:
        """Send the CALCULATED number of wheel notches - one notch per channel row.

        The operator's measurement: the channels are paged (1-20, 21-40, 41-60) and one notch shifts the
        list by one row, so channel 51 needs exactly 8 notches.  The picture is measured as evidence and
        logged, but it does not gate the step: in the field the list HAD scrolled while the captured
        frames were stale ("51 is on screen but failed to click that channel"), and aborting on a stale
        picture turned a working step into a failed run.

        The wheel method is tried in order on the FIRST notch (the method that visibly changes the list
        is kept); later notches just carry on with it.
        """

        total = max(0, int(rows))
        channel_x, channel_y = window_client_point(CHANNEL_FIRST_CLIENT, self._click_size())
        channel_list_box = window_client_box(CHANNEL_LIST_CLIENT_BOX, self._click_size())
        for notch in range(1, total + 1):
            if self.stop_event.is_set():
                return notch - 1
            before = self._capture()[0]
            if not self._wheel_client(channel_x, channel_y, CHANNEL_SCROLL_NOTCHES_PER_ROW):
                LOG.error("auto reconnect: the wheel event for the channel list could not be sent")
                return notch - 1
            if not self._sleep_checked(CHANNEL_SCROLL_WAIT_SECONDS):
                return notch - 1
            after = self._capture()[0]
            change = region_change(before, after, channel_list_box)
            LOG.info("auto reconnect: notch %d/%d sent (the list region changed by %.2f - evidence "
                     "only)", notch, total, change)
            if notch == 1:
                self._pick_wheel_method(before, after, change)
        LOG.info("auto reconnect: sent %d notch(es) = %d row(s) by calculation", total, total)
        self._report("channel", f"已滚动 {total} 行（按计算）")
        return total

    def _pick_wheel_method(self, before, after, change: float) -> None:
        """Keep the wheel method that visibly moved the list (evidence, not a gate)."""

        if change >= CHANNEL_LIST_CHANGE_MIN:
            LOG.info("auto reconnect: the %s wheel moved the channel list (%.2f) - keeping it",
                     self._wheel_method, change)
            return
        method = self._next_wheel_method()
        if method is not None:
            LOG.warning("auto reconnect: the %s wheel did not visibly move the list (%.2f) - trying "
                        "%s for the remaining notches", self._wheel_method, change, method)
            self._report("channel", f"滚轮方式改为 {method}")
            self._wheel_method = method

    def _wheel_client(self, client_x: int, client_y: int, notches: int,
                      method: Optional[str] = None) -> bool:
        """Scroll at a *client* point -> True when the wheel event was sent."""

        point = self._screen_point(client_x, client_y, f"滚轮 {notches} 格")
        if point is None:
            return False
        screen_x, screen_y = point
        chosen = method or self._wheel_method
        LOG.info("auto reconnect: scrolling %d notch(es) at client (%d, %d) = screen (%d, %d), "
                 "method %s, foreground %r", notches, client_x, client_y, screen_x, screen_y,
                 chosen, self._foreground_title())
        if self._wheel_fn is not None:
            return bool(self._wheel_fn(screen_x, screen_y, notches))
        if self.dry_run:
            LOG.info("auto reconnect: DRY-RUN wheel at (%d, %d)", screen_x, screen_y)
            return True
        return wheel_screen(screen_x, screen_y, notches, method=chosen,
                            hwnd=self._game_hwnd())

    def _select_channel(self, channel: int) -> bool:
        """Second select window: click channel 1, SCROLL to the target row, double-click its cell.

        The operator's rule (v0402, after watching channel 51): "there's no need to click [channels]
        before scroll, just click 1, then scroll", and "it should be 8 rows and then the 51 will show
        and then double clicked 51".  For channel 51 the arithmetic is exactly that: 4 channels per
        row means channel 51 is row 13, and with 5 rows visible the list has to scroll 13 - 5 = **8**
        rows for it to come into view.

        The keyboard walk through the channels is GONE.  It walked the selection row by row, which
        produced misleading progress (`列表滚动一行（第 3 行）` - a number about the walk, not about
        the target row) and could never reach a row that was not on screen.  Now:

        1. channel 1 is clicked (one click - it is the top-left cell of an unscrolled list),
        2. the list is scrolled the computed number of rows, each notch verified and MEASURED
           (``_scroll_list_rows``),
        3. the target cell is double-clicked - a single click only highlights a cell,
        4. ``Enter`` (verified by the page change), 2 s, ``Enter`` again starts the login.

        Anything that does not verify is a failure and is reported as one: no Enter is ever sent
        after a channel selection that did not take, because Enter would then confirm whatever
        channel happened to be selected.
        """

        page, score = self.page_now()
        if page is None:
            self._report("channel", f"已在游戏内（{score:.2f}），不再选频道")
            LOG.info("auto reconnect: already in game - the channel step is skipped")
            return True
        if page == PAGE_LOGIN:
            LOG.error("auto reconnect: the channel step needs the channel page, but the login page "
                      "is showing (%.2f)", score)
            self._report("failed", "还在登录页，无法选择频道")
            return False
        size = self._click_size()
        channel_x, channel_y = window_client_point(CHANNEL_FIRST_CLIENT, size)
        select_window = window_client_box(SELECT_WINDOW_CLIENT_BOX, size)
        channel_list_box = window_client_box(CHANNEL_LIST_CLIENT_BOX, size)
        column_pitch = window_client_length(CHANNEL_STEP_CLIENT, size)
        row_pitch = window_client_length(CHANNEL_ROW_STEP_CLIENT, size)
        if not point_in_box(channel_x, channel_y, select_window):
            LOG.error("auto reconnect: channel 1 point %s is outside the select window %s",
                      CHANNEL_FIRST_CLIENT, SELECT_WINDOW_CLIENT_BOX)
            self._report("failed", f"频道 1 坐标 {CHANNEL_FIRST_CLIENT} 不在选择窗口范围内")
            return False

        index = max(CHANNEL_MIN, min(int(channel), CHANNEL_MAX)) - 1
        target_row = index // CHANNELS_PER_ROW
        target_column = index - target_row * CHANNELS_PER_ROW
        rows_to_scroll = max(0, target_row - (CHANNEL_VISIBLE_ROWS - 1))
        # The arithmetic is REPORTED as well as logged: the panel then shows whether the shipped
        # 4-per-row / 5-visible-rows model matches the game the operator is watching.
        self._report("channel",
                     f"{channel}频道 = 第 {target_row + 1} 行 第 {target_column + 1} 列"
                     f"（每行 {CHANNELS_PER_ROW} 个，可见 {CHANNEL_VISIBLE_ROWS} 行）")
        LOG.info("auto reconnect: channel %d is row %d column %d of the %d-per-row list; %d row(s) "
                 "must be scrolled from channel 1", channel, target_row + 1, target_column + 1,
                 CHANNELS_PER_ROW, rows_to_scroll)

        before = self._capture()[0]
        if not self._click_client(channel_x, channel_y, "channel", "频道 1"):
            self._report_click_failure("点击频道 1 失败")
            return False
        if not self._sleep_checked(SELECT_WINDOW_WAIT_SECONDS):
            return False
        after_click = self._capture()[0]
        moved = region_change(before, after_click, channel_list_box)
        LOG.info("auto reconnect: after the channel 1 click the list region changed by %.2f "
                 "(needs %.2f)", moved, CHANNEL_LIST_CHANGE_MIN)
        if moved < CHANNEL_LIST_CHANGE_MIN:
            # A click the game ignored leaves the list without a selection, and then the wheel has
            # nothing to scroll: retried once (not fatal - the arithmetic still drives the clicks).
            LOG.warning("auto reconnect: the channel 1 click did not move the highlight (%.2f) - "
                        "clicking it again", moved)
            self._report("channel", "频道 1 点击没有改变选择，再点一次")
            if not self._click_client(channel_x, channel_y, "channel", "频道 1（重试）"):
                self._report_click_failure("再次点击频道 1 失败")
                return False
            if not self._sleep_checked(SELECT_WINDOW_WAIT_SECONDS):
                return False
            LOG.info("auto reconnect: after the second channel 1 click the list region changed by "
                     "%.2f", region_change(before, self._capture()[0], channel_list_box))

        achieved = 0
        if rows_to_scroll:
            self._report("channel", f"从频道 1 起向下滚动 {rows_to_scroll} 行")
            achieved = self._scroll_list_rows(rows_to_scroll)
            if achieved < rows_to_scroll:
                LOG.error("auto reconnect: %d of %d notches could not be sent (a key/wheel failure), "
                          "not a picture measurement", achieved, rows_to_scroll)
            self._report("channel", f"已滚动 {achieved} 行，第 {target_row + 1} 行应已显示")

        visible_row = target_row - achieved
        cell_x = int(round(channel_x + target_column * column_pitch))
        cell_y = int(round(channel_y + visible_row * row_pitch))
        if not point_in_box(cell_x, cell_y, select_window):
            LOG.error("auto reconnect: channel %d cell (%d, %d) is outside the select window %s",
                      channel, cell_x, cell_y, select_window)
            self._report("failed",
                         f"频道 {channel} 的位置 ({cell_x}, {cell_y}) 不在选择窗口范围内")
            return False
        before_cell = self._capture()[0]
        # The operator's rule for the cell: click it once (that selects/highlights the target channel),
        # then DOUBLE-CLICK it - the double click is what enters the channel.  Enter is not a
        # substitute: "press enter will go into channel1" (it enters the channel the game still has
        # committed, not the highlighted one).
        if not self._click_client(cell_x, cell_y, "channel", f"{channel}频道（单击）"):
            self._report_click_failure(f"单击 {channel}频道 失败")
            return False
        if not self._sleep_checked(CLICK_RETRY_WAIT_SECONDS):
            return False
        chosen = False
        for attempt, gap in enumerate(DOUBLE_CLICK_GAPS, start=1):
            if not self._double_click_client(cell_x, cell_y, "channel",
                                             f"{channel}频道（双击 {attempt}/{len(DOUBLE_CLICK_GAPS)}）",
                                             gap=gap):
                self._report_click_failure(f"双击 {channel}频道 失败")
                return False
            if self.page_verification_available() and self.page_now()[0] != PAGE_CHANNEL:
                # The double click took: the game left the channel window and is entering.
                LOG.info("auto reconnect: the double-click on channel %d made the game leave the "
                         "channel window", channel)
                chosen = True
                break
            if attempt < len(DOUBLE_CLICK_GAPS):
                LOG.info("auto reconnect: the channel window is still there after the %.2f s "
                         "double-click on channel %d - sending the pair again", gap, channel)
                self._report("channel", f"再双击一次 {channel}频道（间隔 {DOUBLE_CLICK_GAPS[attempt]:.2f}s）")
        after_cell = self._capture()[0]
        # The highlight moves INSIDE the list, so the list region is measured as well: on a
        # whole-frame thumbnail a 74x24 highlight is averaged away.  Evidence only - the page check
        # below decides, and the target cell may be the channel-1 cell the game already has selected.
        change = max(region_change(before_cell, after_cell, channel_list_box),
                     screen_change(before_cell, after_cell))
        LOG.info("auto reconnect: after the click and the double-click on channel %d at (%d, %d) the "
                 "screen changed by %.2f (chosen=%s)", channel, cell_x, cell_y, change, chosen)
        if after_cell is None or change < CHANNEL_LIST_CHANGE_MIN:
            LOG.warning("auto reconnect: the click on channel %d at (%d, %d) changed nothing (%.2f) - "
                        "carrying on, the Enter decides", channel, cell_x, cell_y, change)
            self._report("channel", f"点击 {channel}频道 ({cell_x}, {cell_y}) 后画面无明显变化（继续）")
        self._report("channel", f"{channel}频道 已双击选中 ({cell_x}, {cell_y})")

        # The last handoff is intentionally not a one-second Enter loop.  The
        # client needs a quiet interval after channel selection, then receives
        # two Enter taps; repeat that complete round twice more.  Once the
        # yellow character marker appears, the client has already loaded into
        # the map, so no remaining Enter pair is sent.
        before_enter = self._capture()[0]
        for round_number in range(1, CHANNEL_CONFIRM_ROUNDS + 1):
            marker_seen = self._wait_for_channel_confirm_or_marker(
                CHANNEL_CONFIRM_ROUND_WAIT_SECONDS
            )
            if marker_seen is None:
                return False
            if marker_seen:
                return True
            for press_number in range(1, 3):
                reason = self._press("enter")
                if reason:
                    self._report("failed", reason)
                    return False
                if press_number == 1 and not self._sleep_checked(
                    CHANNEL_CONFIRM_DOUBLE_PRESS_GAP_SECONDS
                ):
                    return False
            self._report(
                "enter-confirm",
                f"{channel}频道：第 {round_number}/{CHANNEL_CONFIRM_ROUNDS} 轮双按 Enter",
            )

        after_enter = self._capture()[0]
        change = screen_change(before_enter, after_enter)
        page, score = self.page_now()
        LOG.info("auto reconnect: after %d channel-confirmation rounds the screen changed by "
                 "%.2f and the page is %s (%.2f)", CHANNEL_CONFIRM_ROUNDS,
                 change, self._page_label(page), score)
        if self.page_verification_available() and page is not None:
            LOG.error("auto reconnect: confirming channel %d did not leave the channel page (%s, "
                      "%.2f) after %d double-Enter rounds", channel,
                      self._page_label(page), score, CHANNEL_CONFIRM_ROUNDS)
            self._report("failed", f"确认 {channel}频道 后页面仍是{self._page_label(page)}")
            return False
        self._report("enter", f"{channel}频道")
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
            self._note_frame_size(frame)
            return frame, rect
        try:
            from capture_worker import capture_window

            image, rect = capture_window(self.window_title)
        except Exception:
            LOG.warning("auto reconnect: game window capture failed", exc_info=True)
            return None, (0, 0, 0, 0)
        import numpy as np

        frame = np.asarray(image)[:, :, ::-1].copy()
        self._note_frame_size(frame)
        return frame, rect
        # capture_window returns RGB (PIL); OpenCV templates are BGR/BGR-gray, so the
        # channel order is flipped back here.

    def _note_frame_size(self, frame) -> None:
        """Remember what the last capture measured - the click constants are mapped onto it."""

        try:
            if frame is not None and getattr(frame, "shape", None) is not None and frame.size:
                self._last_frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        except Exception:
            LOG.debug("auto reconnect: the frame size could not be noted", exc_info=True)

    def _client_size(self) -> tuple[int, int]:
        if self._client_size_fn is not None:
            size = self._client_size_fn()
            if size:
                return (int(size[0]), int(size[1]))
        frame, _rect = self._capture()
        if frame is None:
            return REFERENCE_CLIENT
        return (int(frame.shape[1]), int(frame.shape[0]))

    def _press(self, key: str, *, hold: float = KEY_HOLD_SECONDS) -> Optional[str]:
        """Press one key - None when it was delivered, else the reason it was not.

        A bare "not claimed" was useless: the sender refuses a key while live input is
        disarmed (the default until 开始巡逻) or while the game window is not foreground, and
        neither reason could be told apart from the old message.  The reason is returned so
        the caller reports exactly one failure.

        A lost foreground is not treated as final: the assistant's own window can take focus
        back mid-sequence (measured in the field at 22:35:23, where the run stopped with
        `游戏窗口不在前台` even though the game had been in front), so the window is brought
        forward again and the key retried.  ``hold`` lets the login Enter use a longer press.
        """

        sender = self.key_sender
        if sender is None:
            return "没有按键发送器"
        state = self._input_state()
        if state is False:
            LOG.error("auto reconnect: live input is disarmed - key %s was not sent", key)
            return "实时输入未开启（请点击 开始巡逻 启用输入后再试）"
        for attempt in range(1, KEY_REFOCUS_ATTEMPTS + 2):
            if self._window_is_foreground():
                break
            LOG.warning("auto reconnect: game window lost the foreground before key %s "
                        "(attempt %d/%d); bringing it back",
                        key, attempt, KEY_REFOCUS_ATTEMPTS + 1)
            # focus, then click again: a re-focused game ignores keys until it sees a click
            self._activate_window()
            self._sleep(KEY_REFOCUS_WAIT_SECONDS)
        if not self._window_is_foreground():
            LOG.error("auto reconnect: game window is not foreground - key %s was not sent",
                      key)
            return "游戏窗口不在前台，按键被拒绝"
        self._release_modifiers(f"before {key}")
        try:
            try:
                ok = sender.press(key, duration=float(hold), owner="auto-reconnect")
            except TypeError:
                # A sender without the v0411 `owner` parameter (another sender class, a test double):
                # the plain call, so the sequence never depends on that one keyword.
                ok = sender.press(key, duration=float(hold))
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

    def _disarm_input(self) -> None:
        """Turn live input OFF: the patrol state after a failed reconnect."""

        disable = getattr(self.key_sender, "disable_input", None)
        if not callable(disable):
            return
        try:
            disable()
            LOG.error("auto reconnect: live input disarmed because the login failed - patrol is "
                      "stopped (the character is not in game)")
        except Exception:
            LOG.exception("auto reconnect: live input could not be disarmed")

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
            return not (self.stop_event.is_set() or self._cancel_requested.is_set())
        if self._sleep is not time.sleep:
            self._sleep(float(seconds))
            return not (self.stop_event.is_set() or self._cancel_requested.is_set())
        deadline = time.monotonic() + float(seconds)
        while True:
            if self.stop_event.is_set() or self._cancel_requested.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self.stop_event.wait(min(0.05, remaining))


__all__ = [
    "ANCHOR_SPACE_LOGIN",
    "ANCHOR_SPACE_WINDOW",
    "CHANNEL_MAX",
    "CHANNEL_MIN",
    "CHANNELS_PER_ROW",
    "LOGIN_PAGE_MIN_FRACTION",
    "LOGIN_TEMPLATE_NAME",
    "REFERENCE_CLIENT",
    "LoginColourReference",
    "LoginPageEvidence",
    "ReconnectWorker",
    "WORLD_NAMES",
    "analyse_login_page",
    "client_anchor",
    "cursor_screen_position",
    "find_login_page",
    "load_login_reference",
    "login_client_box",
    "login_client_point",
    "login_colour_mask",
    "login_colour_reference",
    "valid_channel",
    "window_client_box",
    "window_client_delta",
    "window_client_length",
    "window_client_point",
    "window_client_scale",
]
