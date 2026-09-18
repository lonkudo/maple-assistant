# 自动重连 (automatic reconnect)

New in this version.  It logs the character back in after the game drops it.

## Current behaviour (v0420) - read this first

This file grew version by version, so the sections below are partly **history**: they say what was tried
and what each field log proved.  The rules that are actually shipped today are:

* **the sequence**: activation click (50, 50) -> login board (773, 324) -> 连接 (854, 400) -> the world row
  `(498 + (w-1)*104, 163)` -> channel 1 (555, 388) -> 8 calculated scroll rows for channel 51 -> one click
  plus a double-click on the calculated cell -> Enter, 2 s, Enter;
* **acts are calculated, pictures are evidence**: the page verifier (two crops per page, minimum score,
  0.60, plus the login page's cream colour) and the region measurements are logged - with every
  signature's score and a saved frame when they miss - but they never abort a step whose clicks are
  arithmetic.  The one hard rule is that **Enter is never sent to confirm a selection that did not move**;
* **a click is sent only** when the window at that point is the game's (its window, process, executable,
  class, title, or its own login UI `igwUserLoginDialog`), the point is inside the game's client
  rectangle, and the window is not ours - our panel is lowered first and the point re-checked;
* **the run owns the machine**: the reconnect takes the keyboard exclusively, releases Alt/Ctrl/Shift
  before every key, clears the automation switch (`reconnect_active`, which the focus gate honours), and
  after a **failed** login leaves live input disarmed so patrol cannot run on the login page;
* **measured space**: client 1366x768, captured frame 1366x768, `screen = client_origin + logical point`,
  no scaling (verified with a cursor round-trip on a 150 % display).

`README.md` (section 自动重连) and `ARCHITECTURE.md` (section 3.1) carry the same contract in short form.

## Signs

Two signs must BOTH be true before anything is pressed:

1. the **掉线** event of the character detector (its yellow marker disappears for several
   frames - the same event the 掉线警报 uses), and
2. the game window must show the login page's **base colour**: `screenshots/login_page_target.jpg`
   is a crop of the page's cream background, so it defines a colour (base + narrow range)
   rather than a shape.  One single region of the window must be that colour over at least
   **6 %** of the window (see the measurement table below).

Ticking the **自动重连** checkbox only ARMS the worker - it never starts the drill.  The drill
starts on a 掉线 event, or when the **测试自动重连（临时）** button is clicked.

## What it does (in this order)

1. brings the game window forward (no refocus, the game keeps the cursor),
2. **arms live keyboard input for the sequence** when it is disarmed (the default until
   开始巡逻/Start Patrol) and puts that state back afterwards - without this the keys are
   refused with `key enter was not claimed`; the panel shows
   `自动重连: 已临时开启实时输入（序列结束后恢复）`,
3. **clicks a harmless spot inside the game client** - client **(50, 50)**, far from every button
   and list entry - because the game ignores keys until its own window has been clicked (measured:
   the window was foreground and `key hold complete=enter` was logged while the login page did
   nothing); the panel shows `自动重连: 已点击游戏窗口 (x, y) 激活输入`,
4. waits **2 s**,
5. **clicks the login BOARD** at client **(773, 324)** - the operator's own click #1, a flat dark
   area of the board (edge density 0.009 vs 0.157 on the glyphs) - and waits **2 s** again,
6. **clicks 连接** at client **(854, 400)** - the operator's click #2, his correction of the
   earlier (907, 420), which sat on the *edge* of the label.  Both points are **refused unless
   they lie inside `LOGIN_BOARD_CLIENT_BOX = (680, 128, 340, 512)`**: v0394 clicked a guessed
   fraction of a wrongly-measured "board" and hit **结束游戏** in the field, and that must never
   happen again (结束游戏 sits at (1120, 582, 145, 55), outside the box, and is unreachable),
7. the 连接 click is **verified by the pixel-change test**; when the game ignores it, the worker
   locates the button itself with OpenCV (the largest *saturated* button-shaped rectangle on the
   board, `CONNECT_BUTTON_*`, with `recording-assets/login_connect_target.jpg` matched as a
   template first when one exists) and otherwise presses **Enter** (up to **3** attempts, each
   held **0.15 s**), failing with `登录页没有反应（Enter 按了 3 次仍未生效）` if nothing moves.
   Every candidate box and score is logged, so the search can be tuned from the log alone.
   **No click is ever sent on a guess**,
8. waits **5 s** for the login,
9. first select window: **clicks the row of the chosen world** - measured on his own frame
   (`screenshots/channel_select_first.jpg`): the five worlds are in **one horizontal line** at
   y=163, 104 px apart, so world w is clicked at client `(498 + (w-1)*104, 163)`.  This replaced
   the old `down` walk, which did nothing on a horizontal list and left the default 蓝蜗牛
   selected - that is why choosing 蘑菇仔 logged in as 蓝蜗牛.  If the click does not change the
   world row, the keyboard fallback uses `right` presses and Enter, and waits **2 s**,
10. second select window - **click channel 1, then scroll, then double-click the target cell**:
    channel 1 is clicked at client (555, 388), the list is **scrolled** to the target row, and that
    cell is **double-clicked** (a single click only highlights a cell).  Then **Enter**, waits
    **2 s**, then **Enter** again (the second one starts the login).

    The arithmetic behind the scroll, with the operator's own example: **4 channels per row** and
    **5 rows** visible at scroll top 0 means channel 51 is **row 13 column 3**, so the list has to
    scroll **13 - 5 = 8 rows** and the cell to double-click is (743, 510).  The full arithmetic is
    printed in the panel (`51频道 = 第 13 行 第 3 列（每行 4 个，可见 5 行）`), so a channel model that
    does not match the game is visible immediately.

### Why there is no keyboard walk any more (v0402)

The operator watched a run for channel 51 and asked: *"why is that the algorithm say 滚动3行?  it
should be 8 rows and then the 51 will show and then double clicked 51, and there's no need to click
[channels] before scroll, just click 1, then scroll"*.  What he saw was the old **keyboard walk**:
it moved the selection down row by row and reported `列表滚动一行（第 3 行）` - a number about the
walk, not about the target row - and it could never reach a row that was not on screen.  It is gone.

The scroll is now **closed-loop and measured** (`_scroll_list_rows`):

| step | what happens |
|---|---|
| one notch at a time | `CHANNEL_SCROLL_NOTCHES_PER_ROW` (= 1) notch per row, never one big scroll |
| each notch verified | the list region must change within `CHANNEL_LIST_SETTLE_ATTEMPTS` checks; a swallowed notch is re-sent |
| each notch measured | `list_scroll_rows` matches the rows against the frame from before the notch, so "one notch = one row" is a MEASUREMENT, not an assumption |
| the result is reported | `已滚动 8 行，第 13 行已显示`, and a scroll that does not get there fails with `频道列表只滚动了 N/8 行` |
| the double-click is verified | a click the game ignored fails with `双击 51频道 (743, 510) 没有反应，未确认频道` and sends **no Enter** - Enter would confirm whatever channel happened to be selected |

The one exception: when the target cell IS the channel-1 cell (channel 1 itself) no highlight change
can be expected, so that single case proceeds on the already-selected channel - and the confirm
Enter is still verified, so a wrong channel still fails loudly.

Every click is clamped to `SELECT_WINDOW_CLIENT_BOX = (292, 59, 731, 507)`, so a wrong constant
can never click somewhere else in the game.

## Why a scroll is measured on the LIST, not on the whole window (v0401)

The operator's report: *"on scroll the channel row the row is changed but you detected it as not
scrolled"*.  The wheel was fine - the **measurement** was wrong.  `screen_change()` compares a
64x36 grey thumbnail of the whole 1366x768 frame, and one channel row is 30 px: at that scale the
shift moves the thumbnail by about **one pixel** and the only thing that really changes is the
*text* inside the row, which the downscale averages away - so a scroll that had happened measured
far below `SCREEN_CHANGE_MIN = 2.0`.

Every channel-list decision therefore measures the list region itself:

| constant | value | meaning |
|---|---|---|
| `CHANNEL_LIST_CLIENT_BOX` | (498, 340, 500, 226) | the 4-per-row channel grid, inside the window clamp |
| `CHANNEL_LIST_CHANGE_MIN` | 1.0 | a moved highlight or a one-row scroll clears this on that crop |
| `CHANNEL_SCROLL_WAIT_SECONDS` | 1.0 | first pause before the first repaint check |
| `CHANNEL_LIST_POLL_SECONDS` | 0.30 | between the following checks |
| `CHANNEL_LIST_SETTLE_ATTEMPTS` | 6 | ~2.5 s of repaint checks before "the list did not react" |
| `CHANNEL_SCROLL_ATTEMPTS` | 2 | a wheel can be swallowed while focus settles - it is re-sent |
| `CHANNEL_SCROLL_SLACK_NOTCHES` | 4 | extra notches the closed-loop scroll may spend |
| `CHANNEL_SCROLL_MATCH_ROWS` / `_MIN_SCORE` | 3 / 0.40 | how the per-notch shift is measured (`list_scroll_rows`) |
| `WORLD_LIST_CLIENT_BOX` / `WORLD_LIST_CHANGE_MIN` | (430, 110, 590, 110) / 1.0 | the same treatment for the world row |

The game also finishes a scroll a few frames *after* the wheel event, and the patience above is
why "the list did not react" is now a real measurement instead of one capture 0.4 s later.
`test_reconnect_worker.ChannelListMeasurementTests` pins both halves of this: a synthetic one-row
scroll that the whole-frame measure misses and the list measure sees.

## Which window owns the click point (v0403, corrected in v0404)

The operator's field report: *"the window keeps losing focus to assistant, so that the click is not
successfully emitted to the game"*.  Two things can produce that:

1. **a click on a point the ASSISTANT owns.**  Clicking such a point activates the assistant, so the
   game loses the foreground on every attempt and never sees the click.  Every click point is
   checked first (`_point_owner`, `WindowFromPoint` + the ROOT ancestor + the owning process id): if
   the assistant owns the point the game is brought forward **once** and the point re-checked; if it
   is still the assistant's, **no click is sent** and the panel says so -
   `点击位置 (x, y) 属于助手窗口「…」`.  It never blocks a click it cannot judge (no pywin32, no
   game handle, `--dry-run`), and a failed click is reported **once**, with the precise reason.
2. **the 掉线 alarm overlay.**  The alarm is a topmost layered window drawn over the whole virtual
   desktop - and it was created **without `WS_EX_TRANSPARENT`**, so every click inside it was
   swallowed by the overlay and never reached the game.  The reconnect starts on the *same* 掉线
   event as that alarm, so the two overlap exactly.  All three alert overlays are now
   `WS_EX_LAYERED | WS_EX_TRANSPARENT` (click-through): they stay visible and no longer take the
   mouse.

### Why only the assistant's windows are refused (the v0403 lesson)

v0403 refused every point whose window was not the captured game window, and the field log showed
why that was far too strict:

    17:14:14 auto-reconnect ERROR the point for 登录面板 at screen (1322, 593) still belongs to
    'igwUserLoginDialog' (hwnd 327856, game hwnd 263702, foreground '冒险岛怀旧服')

`igwUserLoginDialog` is the **game's own login dialog** - a separate top-level window of the game
that covers the window the reconnect captures - so refusing it blocked the whole sequence.  The
rule is now, in order:

| the window at the point | result |
|---|---|
| the captured game window | accepted |
| the game's **process** (`igwUserLoginDialog` and friends) | accepted, logged at INFO |
| the game's window class, or a title that contains the game's title | accepted |
| **the assistant's own process** | **refused** after bringing the game forward once - this is the bug that was reported |
| any other, unidentifiable window | accepted with a WARNING (it must never block a click we cannot name) |

The run also says it out loud when the foreground is lost: after every click the worker re-checks
the foreground window and logs `after clicking … the game is NOT foreground any more (foreground is
…)`, and the run start logs the whole mapping in one line -
`client origin on screen (x, y); foreground window '…'; game hwnd …`.  Every click is
"client origin + client point", so that line is what a wrong mapping would show.

`work/point_probe.py` answers the same question from outside the assistant: it prints the game
window's rect, client origin and size, the foreground window, and for every measured point the
client -> screen conversion plus the window that owns that screen point **and its process id**
(who it belongs to: the assistant, the game, or somebody else), moving the cursor there for a
moment so it can be seen.  Nothing is clicked; the mouse is only moved.

## Every step is verified by the PAGE and retried (v0405, corrected in v0406)

The operator: *"you didn't make a verifier for each step, this time the first step is failed but you
didn't notice and retry.  the first page contains element 连接 a deep brown login board and 结束游戏,
the second one contains 蓝蜗牛 or other options, the third one should show channel"*.

The game shows **three pages** and every step now ends by asking which one is on screen.  Each page is
identified by **two small, glyph-dense patches** and its score is the **minimum** of them, so every
patch has to match:

| page | patches (client box) | login | world | channel | in game |
|---|---|---|---|---|---|
| 登录页 | 连接 label (780,380,160,40) + 结束游戏 label (1060,565,190,45) | **0.995** | 0.039 | -0.022 | 0.068 |
| 世界选择页 | empty grid (500,368,420,40) + (500,400,420,60) | -0.102 | **0.999** | 0.075 | 0.044 |
| 频道选择页 | channel grid (500,368,420,40) + (500,430,420,80) | -0.102 | 0.075 | **0.997** | -0.002 |
| (none of them) | in game - the answer the end of the sequence must prove | | | | |

`PAGE_MIN_SCORE = 0.60` therefore separates them with a wide margin, and **no page matches another
page** (the test `test_no_page_signature_matches_another_page` pins exactly that).

### The v0405 lesson: one big patch is not a signature

v0405 used a single 340x512 crop of the login board.  A patch that size is dominated by background,
so it scored **1.000 on the world page as well** - a coin flip that could report the world page while
the login page was still on screen.  That is the operator's follow-up report: *"the verifier broke
(login not success but going into the second stage)"*.  Two further changes make that impossible:

1. the **board click** (a harmless "give the game a click" action) may only be confirmed by the PAGE,
   never by a pixel change - v0405 let a highlight/hover pixel change count as success;
2. a step that does not reach its page **fails the run instead of continuing**:
   `登录步骤失败 3 次，页面还是登录页（连接 / 结束游戏）（1.00）`.  The world step is then never reached.

### Waiting for the game (v0406)

The operator: *"the process is stepping too quick, the game isn't react that quick"*.  After every
action the page is now **polled every 0.5 s** instead of sampled once, and the operator's own pauses
still come first (5 s after the login Enter, 3 s per step):

| step | action | verified by | first pause | poll until |
|---|---|---|---|---|
| 1 | activation click | - | 3 s | - |
| 2 | click the login board | page becomes 世界选择页 (page only!) | 3 s | 6 s |
| 3 | click 连接 | page becomes 世界选择页 | 5 s | 6 s |
| 4 | login-page Enter (one per attempt) | page becomes 世界选择页 | 5 s | 6 s |
| 5 | click the world row | page becomes 频道选择页 | 3 s | 6 s |
| 6 | click channel 1, scroll, double-click the cell | the list/cell changed | - | per notch |
| 7 | Enter | page becomes none (in game) | 3 s | 6 s |

A step is retried up to `PAGE_STEP_ATTEMPTS = 3` times, is **skipped when the page is already past
it**, and needs `_await_page` to see the change within the timeout - so a slow game slows the run
down instead of failing it, while a game that never reacts still fails loudly.

## The assistant must not be in the way, and the point must be inside the CLIENT (v0407)

The operator: *"click login board step always failed, it will go to click the title of the game
window"*.  Two different problems can look like that, and both are handled now:

1. **a point our OWN window covers.**  The earlier report was *"the window keeps losing focus to
   assistant"*, and the two together say the assistant's panel sits over that point: the guard
   refuses the click (correct - it would activate the assistant), so the board step failed and the
   run stopped on it.  Before giving up, the worker now calls `_lower_own_windows()`: every visible
   top-level window of the assistant's own process is pushed to the BOTTOM of the z-order
   (`SetWindowPos(HWND_BOTTOM)` with `SWP_NOACTIVATE`, plus `HWND_NOTOPMOST` for an overlay), so the
   panel stays exactly where it is and simply stops covering the game.  The game is then brought
   forward again and the point re-checked.
   **And the board click is no longer fatal:** it only exists to "give the game a click", so when it
   cannot be sent the run says `登录面板没能点击，继续点「连接」` and goes on to 连接 - the click that
   matters - instead of ending there.
2. **a point on the window TITLE BAR.**  The title bar and the borders belong to the *same top-level
   window* as the game content, so "which window owns this point" cannot exclude them - a click there
   activates or even DRAGS the window and the game never sees it.  Every click point is therefore also
   checked against the game's **client rectangle** (`_client_rect_on_screen` +
   `point_in_rect`); a point inside the window but outside its client is refused with
   `点击位置 (x, y) 不在游戏客户区内（客户区 (l, t, r, b)）`.

Both refusals log the whole geometry in one line (`client (x, y) -> screen (x, y); game hwnd …;
window_rect …; client_origin …; cursor …; foreground …`), so a wrong mapping is readable from the log
alone.

### `work/point_probe.py` answers it from outside in one run

With the game open:

```bat
py -3.10 work\point_probe.py             :: report + park the cursor on every point
py -3.10 work\point_probe.py --click     :: also really click them
```

For each measured point it prints the client point, the screen point, **the cursor position read back
after the move** (a mismatch means the coordinate spaces disagree - a DPI problem - and every click is
off), the window that owns that point with its process id and whether that is the assistant, the game
or another process, and whether the point is inside the game's client rect and how deep.  It also
prints the game's window rect (and the title-bar band), the client origin and size, the foreground
window, and the system/window DPI.

## Focus: who steals it, and clicking an INACTIVE game (v0408)

The operator: *"still error, there's must be sth that steal focus of the game thus causing login board
click failed"*.  A game ignores mouse clicks while it is not the **active** window, so a click can land
on exactly the right pixel and still do nothing.  Three things follow.

### 1. The run now NAMES the thief

`_FocusWatch` samples the foreground window every 100 ms for the whole run and logs every change with
the window title, its hwnd, its process id and whether that process is the assistant's own:

    auto reconnect: foreground changed: 「todo_helper (0408)」 hwnd 1234 pid 5678 (助手自己)
                     -> 「冒险岛怀旧服」 hwnd 91011 pid 1213 (别的程序)

The failure reports carry the count too (`登录步骤失败 3 次，页面还是…；焦点被抢 2 次: …`), so the
panel and `error.log` say who took the focus instead of leaving it a mystery.

### 2. Every click takes the focus in the last moment and is verified

* `click_screen(..., keep_focus=<game hwnd>)` makes the game active immediately before the mouse
  events (no more "the click went out while something else was active").
* After the click the worker checks that the game is still active.  It is **not** accepted any more
  when it is not: the focus is taken back and the click is repeated.

### 3. The game's OWN dialogs count as active

This is the likely thief, and the field log had already named it:

    17:14:14 the point for 登录面板 ... belongs to 'igwUserLoginDialog'

`igwUserLoginDialog` is the game's own login dialog - a **separate top-level window** of the game.  When
*it* holds the foreground, the old check (configured title / cached hwnd) said "the game is not
foreground", so a click the game had received perfectly well was treated as lost: the worker took the
focus back, clicked again, and the step failed - *"click login board step always failed"*.
`_game_is_active()` now accepts **any window of the game's process** as active for clicking (a click is
delivered to the window under the cursor, which is the game's dialog).  The keyboard path still
requires the real game window, because keys go to the focused window.

## The channel-list scroll: three wheel methods, and hands off the automation (v0409)

The operator's 19:07 log is unambiguous about what was wrong - twelve wheel notches and the list never
moved:

    第 1 次滚动（还差 8 行） changed the channel list by 1.27 (check 1)
    the list moved 0 px (0.00 rows, match 0.95)
    notch 1 changed the list but did not move it (0.00 rows) - scrolling again
    ... (notches 2-12 the same) ...
    the channel list scrolled 0 of 8 requested rows (12 notches)

The change was *seen* (a hover highlight moves) but the list never shifted, so the wheel event did not
reach the game at all: the legacy `mouse_event(MOUSEEVENTF_WHEEL)` is filtered by many games.  The
wheel is now tried **three ways** and the one that MOVES the list is kept for the rest of the run,
because every notch is verified anyway:

| order | method | why |
|---|---|---|
| 1 | `SendInput` with MOUSEEVENTF_WHEEL | the modern replacement for mouse_event |
| 2 | `PostMessage(WM_MOUSEWHEEL)` to the window under the cursor | this is what a window-procedure based (GDI) list actually reads |
| 3 | legacy `mouse_event` | last resort |

A notch that does not move the list switches to the next method **without spending the notch**, and the
panel shows `滚轮方式改为 postmessage`.

### The channels are PAGED (the operator's model)

Page 1 is channels **1-20**, page 2 is **21-40**, page 3 is **41-60** - five rows of four per page, and
one wheel notch is one row (so five notches turn a page).  Channel 51 is therefore still row 13 and
still needs **8** rows of scroll, and the target is then selected exactly as the operator described:
*"calc the channel pos and click it three times (first one click, then double click)"*.

### The automation stands down for the run

His hypothesis was *"the focus lost is because patrol start (自动化启动) steal that focus"*.  In that
particular run the focus was **not** the problem - every click logged
`foreground '冒险岛怀旧服'`, and the only foreground change came 4 s **after** the failure - but the log
did show the real interference:

    attack repetition: a / key-down=a owners=1 / key-up=a owners=0      (every second, all run long)

The reconnect arms live input (it needs it for its own keys) and that is the same switch the attack
worker watches, so the automation woke up and typed `a` through the whole sequence.  The run now clears
the automation event for its duration (`已暂停自动化（巡逻/攻击），本机接管输入`) and restores it
afterwards, exactly the way it already handles live input.

### And from the same log: the page verifier works

    auto reconnect: 点击 蘑菇仔 世界行 reached 频道选择页 after 0.5 s (score 0.82)
    auto reconnect: after the channel 1 click the screen changed by 0.37

so the login, world and channel steps were all confirmed by the page measurement, and the run stopped
only at the scroll.

## A click is only sent when the GAME owns the point (v0410)

The operator: *"still lose focus at first step is that you click a pos outside of game window?  check
the code"*.  Reading `_point_owner` showed the leak - its last rule was

    * any other, unidentifiable window -> accepted with a WARNING, never refused

and that is precisely how the foreground gets handed away: a click on any window that is not the game
activates *that* window.  The game's own dialog (`igwUserLoginDialog`) is already covered by the
"game's process" rule, so the only windows left in that branch are FOREIGN ones - a notification, an
overlay, an IME, another application.

A point is now clicked only when it belongs to

| the window at the point | result |
|---|---|
| the game's window | click |
| a window of the **game's process** (its own dialogs, `igwUserLoginDialog`) | click |
| the game's window class, or a title that contains the game's title | click |
| a window of the ASSISTANT's process | **refused** (it would activate the assistant) |
| **any other window** | **refused** (it would activate that window) |

and inside the game it must also be inside the game's **client rectangle**, never on its title bar or a
border.  Every refusal names the window **and lists the whole stack over that point**
(`windows_over_point`: title, hwnd, process id, topmost, click-through), so a third-party overlay is
visible in the log instead of being a mystery:

    点击位置 (599, 319) 属于助手窗口「todo_helper (v0410)」（请让游戏窗口保持在前台、不要被助手窗口挡住）
    Windows over that point: 「todo_helper (v0410)」 hwnd 1378514 pid 6092 topmost | …

When a click IS sent, the log says who receives it (`belongs to the game's own window … - click
accepted`), so "the click went somewhere else" can no longer happen silently.

## Who steals the focus: a held Alt, and the automation typing during the run (v0411)

The operator: *"why is that the game keep losing focus?  some times a folder steal its focus sometimes
other application, is that the patrol started wrongly and wire wrong key input?"* - and yes, that is
exactly the mechanism, from the code:

* **Alt is this game's JUMP key.**  `status_worker._MOVEMENT_KEYS` contains `"alt"`, and the
  random-jump / stair-jump paths tap it while patrol runs.
* **A held Alt turns a later key into a Windows shell chord**: `Esc` becomes **Alt+Esc** (switch to the
  next window - *"a folder steals its focus"*), `F4` becomes **Alt+F4** (`shutdown_worker` sends that
  very chord to close the game), `Tab` becomes Alt+Tab.
* The reconnect must **arm live input** for its own keys, and that is the same switch every other key
  producing worker waits for (`attack`, `random-jump`, `channel_switch`, whose patch even starts with
  `["esc", "enter"]`), so they all wake up in the middle of the login and select pages.  The 19:07 log
  shows it: `attack repetition: a` every second and `random jump skipped: motion arbiter refused`.

Two guards now:

### 1. The reconnect owns the keyboard

`WindowKeySender.begin_exclusive("auto-reconnect")` / `end_exclusive(...)`: while a run is active the
sender **refuses key injections from every other caller** (`blocked key=a: the keyboard is owned by
'auto-reconnect'`), so the attack, jump and channel-switch workers cannot type into the game at all.
A key-up is never blocked - releasing is always safe.

### 2. No modifier may stay down

`WindowKeySender.release_modifiers()` force-releases Alt/Ctrl/Shift, both the keys the sender believes
are down and the ones **Windows itself reports** down (`GetAsyncKeyState`).  The reconnect calls it

* once when the run starts,
* **before every key it sends** (so its Enter can never become Alt+Enter or Alt+F4),
* once when the run ends.

Every release is logged (`modifiers released by auto reconnect (run start): alt`), so a stuck Alt
becomes visible instead of turning into a window switch.

## Patrol must not run on the login page (v0412)

The operator: *"why is that patrol is active?  it shouldn't be activated on reconnect stage, the
character isn't successfully login.  fix this issue"* - and the reason it stayed active was in
`focus_worker.py`, which re-arms it every 0.2 s:

    active = bool(input_enabled and game_focused)
    if active:
        self.automation_active_event.set()

So clearing the automation switch inside the reconnect was undone 200 ms later (live input is armed for
the sequence, the game is foreground), and attack/jump started typing on the login page again.  Three
changes:

1. **`reconnect_active` is now a first-class event.**  The focus gate keeps the automation switch
   CLEARED while it is set (`reconnect_active_event`), so nothing can re-arm patrol behind the
   reconnect's back.  `assistant.py` creates it and hands it to both the focus worker and the
   reconnect worker.
2. **The automation is stood down FIRST**, at the very top of `_run_sequence` - before the login-page
   colour gate, which waits up to 12 s.  Until now the attack/jump workers kept typing for those first
   seconds of a 掉线.
3. **A failed login STOPS the patrol** instead of resuming it.  On success the input state and the
   automation are restored as they were (patrol continues where it left off); on failure the live input
   is **disarmed** (`_disarm_input`), which is also what makes the focus gate keep the automation
   cleared by itself, and the panel says
   `登录没有成功：已停止巡逻（不会在登录页乱按键），请手动处理后再开始巡逻`.

Verified with a smoke script (`work/smoke_automation_lifecycle.py`):

| | automation after | live input | reconnect flag |
|---|---|---|---|
| successful login | **True** (patrol resumes) | as before | cleared |
| failed login | **False** (patrol stays stopped) | **disarmed** | cleared |

The same run also showed every guard firing: `modifiers released by auto reconnect (run start / before
enter / run end)` and the keyboard released at the end.

## `igwUserLoginDialog` IS the login UI (v0413)

The operator's 19:40 log named everything:

    (1341, 751) belongs to 'igwUserLoginDialog' (hwnd 1246804) - neither the game nor the assistant,
    so the click is REFUSED.
    Windows over that point: 「igwUserLoginDialog」 hwnd 1246804 pid 5644 |
    「冒险岛怀旧服」 hwnd 263702 pid 4288 | 「Clash for Windows」 … | 「Program Manager」 …
    client (773, 324) -> screen (1341, 751); game hwnd 263702
    window_rect (560, 396, 1942, 1203); client_origin (568, 427)

`igwUserLoginDialog` is a **separate process** (pid 5644 against the game's 4288), which is why the
"game's process" rule never matched it - and it is the window the login board is drawn on (**连接** is
on it).  Refusing it blocked the whole login.  The missing rule is geometric: that window lies
**inside the game window's rectangle**, because it is the game's own login UI, drawn by its login helper
over the game window.

The rule, in order:

| the window at the point | result |
|---|---|
| the game's window, the game's process, the game's class, the game's title | click |
| **our own process** | **refused** (a click there activates the assistant) |
| **inside the game window's rectangle and at least a quarter of its area** | click - the game's own login UI (`igwUserLoginDialog`) |
| anything else | **refused**, and the whole window stack over that point is logged |

A foreign application (Clash, an IME, the desktop) is never fully inside the game window, and the
assistant is refused by process id before this rule is even reached.

### The login page is also recognised by its COLOUR

The 19:42 log showed the page measurement at **0.51** - under the 0.60 threshold - so the login step
could not be verified while the login page was on screen.  The shipped cream reference
(`screenshots/login_page_target.jpg`, one region >= 6 % of the window) is an independent signal for
exactly that page, so `page_now()` now answers:

1. a crop signature match wins when there is one;
2. otherwise, if the login page's **colour** covers a large region, the page is 登录页;
3. otherwise the answer stays "none of the three" (in game).

### A page failure now leaves evidence

When a page measurement does not reach the threshold, the worker logs **every signature's score**
(`page evidence for …: 世界选择页=0.512, 登录页=0.043, …`) and saves the frame
(`screenshots/page_*.jpg`), so what the classifier saw is readable instead of guessed.

## Only the assistant's own window is refused (v0414)

The operator corrected the whole chain of v0410-v0413: *"it is in the same game window, they have the
same pid - why did you divide it into two processes"*, and *"it just is the Maplestory_Classic.exe"*.
The login board (with **连接** and **结束游戏**) lives in `igwUserLoginDialog`, which belongs to the game -
so every rule built on the window handle, the process id, the executable name or the class was wrong,
and each version refused the very clicks the login needs:

    19:46 (v0413): (1454, 753) belongs to 'igwUserLoginDialog' - neither the game nor the assistant,
    so the click is REFUSED      -> the 连接 click never happened and the login never started

The rule is now minimal and geometric:

| the window at the point | result |
|---|---|
| **the assistant's own process** | **refused** - this is the one window that really steals the foreground |
| anything else | **clicked**, and *warned about* when it is not the game's main window (the whole stack over that point is logged) |

and the real guard is the check that was always there: the point must lie inside the game's **client
rectangle** (`_client_rect_on_screen`), never on its title bar or a border - that is the case the
operator actually hit (*"it will go to click the title of the game window"*).

The game's own login client also counts as "the game" for the two checks that would otherwise treat a
good click as lost:

* `_game_is_active()` - after a click on the login UI, that window IS the foreground, so the click is
  not retried forever;
* `WindowKeySender._foreground_matches()` - the login UI is the game's own input surface, so keys sent
  while it is focused are not refused.

### Evidence instead of guessing

When a page measurement misses its threshold, the worker logs every signature's score
(`page evidence for 点击「连接」: 登录页=0.512, 世界选择页=0.043, …`).  Diagnostic frames from a real
window are saved to `screenshots/page_*.jpg`; with an injected capture (tests) only the scores are
logged, so nothing pollutes the folder.

## One window for the picture, the clicks and the checks (v0415)

The operator: *"failed again the cursor clicks outside of the game window - did you take the game window
POSITION into consideration?"*, and *"under Maplestory_Classic there are 6 subprocesses; 冒险岛怀旧服 is
the game window"*.

Reading the code showed a real split:

* the **picture** comes from `capture_worker.capture_window(window_title)` -> `FindWindow(None, title)`,
  the first top-level window with that title;
* the **geometry checks** (`_game_hwnd`, `_client_rect_on_screen`, `_point_owner`) used the key
  sender's handle, which `select_window()` had resolved separately.

With several windows/processes under the game, those two can be DIFFERENT windows - and then every
click is computed from one window's position and judged against another's, which is exactly "the cursor
clicks outside the game window".

The reconnect now ties them together:

1. `find_game_windows(title)` lists every visible top-level window with that title, with its **class,
   process id, executable, window rectangle, client origin and client size**;
2. the run picks the candidate whose **client size equals the captured frame** - self-verifying, because
   the frame IS the client area of the window it came from - and logs the choice
   (`the captured 1366x768 frame belongs to hwnd … - every click is computed from that window`);
3. `_game_hwnd()` returns that same window, so the client-rect and ownership checks describe the very
   window the coordinates were measured in;
4. if NO candidate's client size matches the frame, that is logged as an **ERROR** with the whole list,
   because it means the picture and the clicks come from different windows;
5. every click logs the window it is computed from (`激活 uses hwnd … class … rect … client origin …`)
   and `click_screen` **reads the cursor back** after moving it, warning when the pointer is not where
   the sequence asked for it.

## The assistant's own window is refused FIRST (v0416)

The operator: *"the cursor will automatically click to the 自动重连 button, thus causing the error"*.

That was a real hole in `_point_owner`: the "our own process" test sat **after** the
"the game window could not be resolved -> accept" early return.  So whenever the game handle was not
resolved (title mismatch, a minimised window, the frame not yet measured), the point was accepted and
the click landed on whatever was there - including the panel, where it toggled the 自动重连 checkbox
(which disables the worker mid-run: "auto reconnect disabled" in the log) or pressed
测试自动重连 again.

The rule is now ordered so the ONE forbidden window is checked first, and it needs no game handle:

| step | the window at the point | result |
|---|---|---|
| 1 | **our own process** | **refused**, logged as `is OUR OWN window … - the panel must not be clicked` |
| 2 | the game handle could not be resolved | accepted (nothing else can be compared, rule 1 already passed) |
| 3 | the game's own window | accepted |
| 4 | anything else | accepted, warned about (the game's own login UI lives in such a window) |

plus the client-rectangle check: the point must be inside the game's client area, never on its title bar.

When a point belongs to our own window, `_screen_point` calls `_lower_own_windows()` (the panel is
pushed to the bottom of the z-order - it does not move or resize) and tries again, so a panel covering
the game's top-left corner no longer blocks the activation click.  If the point is *still* ours, **no
click is sent** and the panel says which window owns it.

## The pace (deliberately slow)

The operator asked for a slower pace between the operations, and a game that is still loading
simply swallows a click or a key:

| constant | value | where |
|---|---|---|
| `ACTIVATE_CLICK_WAIT_SECONDS` | 3.0 | after the activation click and after the board click |
| `SELECT_WINDOW_WAIT_SECONDS` | 3.0 | after opening each select window, and after the world click |
| `LOGIN_WAIT_SECONDS` | 5.0 | after the login Enter |
| `CHANNEL_CONFIRM_WAIT_SECONDS` | 2.0 | between the channel Enter and the login Enter |
| `KEY_DELAY_SECONDS` | 0.60 | between two keyboard moves in a list |
| `CLICK_RETRY_WAIT_SECONDS` | 1.00 | after every click (and between the two halves of a double-click: 0.12 s) |
| `KEY_REFOCUS_WAIT_SECONDS` | 0.50 | after bringing the game window back before a key is retried |

A full run is therefore ~25 s of waiting (19 pauses, the longest 5 s), which is intentional: the
operator's own rule is
"after the login enter pressed we need to wait 5 s before going to the next step, each step we
need to wait 2 s before proceed".

Why the 连接 click is verified: measured in the field (v0391, 22:35) the Enter was reported delivered, the
login page stayed on screen, and the rest of the sequence (the world click, the channel moves) was
typed into the page while the run reported progress.  The check compares pixels rather
than re-running the colour gate, because the next screen the game shows is the world list - also
cream-themed - so "is the page colour still there" cannot tell the two apart.

A key whose foreground was stolen mid-sequence (measured: the assistant's own window took focus at
22:35:23) is now retried after the game window is brought forward, instead of ending the run with
`游戏窗口不在前台`.

Every step is reported in the 附加功能 panel next to the 自动重连 selection
(`自动重连进行中 - world: 绿水灵 (row 3)`, `自动重连完成: 绿水灵 8频道`, or
`自动重连失败: …`), and **every failure is logged at ERROR level**, so it lands in
`error.log` (which receives ERROR and above only).  Each failure names its step:

| report | meaning |
|---|---|
| `找不到游戏窗口 …` / `游戏窗口没有切到前台` | step 1 - the window could not be brought forward |
| `找不到登录页颜色参考图 login_page_target.jpg` | no colour reference (screenshots/ or recording-assets/) |
| `游戏窗口不是登录页（同色区域 …）` | the colour gate measured too little of the page colour |
| `实时输入未开启（请点击 开始巡逻 启用输入后再试）` | the sender refused the key and could not be armed |
| `游戏窗口不在前台，按键被拒绝` | focus was lost and could not be taken back (now retried first) |
| `按键 enter 未被接受（游戏窗口没有接管键盘）` | the sender rejected the key for another reason |
| `登录页没有反应（Enter 按了 3 次仍未生效）` | the frame never changed after Enter - the game ignored the key |
| `实时输入未开启…` / `游戏窗口不在前台…` | the keys could not be delivered (arming/focus) |

## Testing it without waiting for a 掉线 (temporary)

The 附加功能 panel has a temporary **测试自动重连（临时）** button under the 自动重连
status line.  It runs exactly the sequence above, right now, and deliberately ignores the
enable checkbox - that is the point of the button: try the drill before arming the automatic
trigger.  It also **measures** the login-page colour and shows the number
(`登录页颜色检测: 同色区域 50.8%（阈值 6.0%），判定为登录页`), so the gate can be checked
from the panel without waiting for a real 掉线 - the manual run is not blocked by that
number, while the automatic 掉线 path waits for it.  The button is disabled while a run
is going on and re-arms itself when the worker reports `done` / `failed`.  Remove it (and
this section) once the sequence is confirmed in game.

## Settings

* checkbox **自动重连** - arm/disarm the feature (arming alone does nothing),
* **world** - 蓝蜗牛 / 蘑菇仔 / 绿水灵 / 漂漂猪 / 小白兔,
* **频道** - an integer 1-60 only (validated in the box and again when applied).

All three are saved with the other 附加功能 settings and restored on the next start.

## The login-page reference is a COLOUR (measured)

`login_page_target.jpg` is a 200×200 crop of the login page's cream background.  Correlating
it as a shape is hopeless (it has almost no structure), but as a **colour reference** it is
exactly right: its per-channel median is the base colour (measured **BGR 213,235,241**) and
its own 1st-99th percentiles define the narrow range (**194-230 / 219-249 / 225-253**,
widened by 6 levels) - so the range keeps the crop's texture instead of one flat value.

Measured on the operator's own frames with that range:

| frame | largest single region of the page colour |
|---|---|
| `channel_select_first.jpg` / `channel_select_second.jpg` (1366×768) | **11.7 %** (one 787×673 region) |
| `second_window.jpg` (1366×768, in game) | 0.1 % |
| `frame_000010.jpg` (1080×768, in game) | 0.0 % |
| `1.jpg` (in-game crop) | 0.0 % |

So `LOGIN_PAGE_MIN_FRACTION = 0.06` separates the login/select pages from gameplay with a
wide margin, and no gameplay frame comes close.  The worker logs the measured value on both
the success and the failure path, so the number can be tuned from the log alone
(`work/login_colour_probe.py` re-runs the same measurement over the whole folder).

### Replacing the reference

```bat
:: with the LOGIN page on screen: drag a box round a flat area of its background
.venv\Scripts\python.exe work\reconnect_capture.py --login

:: with the WORLD-select window on screen
.venv\Scripts\python.exe work\reconnect_capture.py --first

:: with the CHANNEL-select window on screen
.venv\Scripts\python.exe work\reconnect_capture.py --second
```

A flat crop is now the *right* thing to save for `--login` (the worker reads its colour, and
the wider the flat area, the better the base colour).  `--first` / `--second` save the whole
1366×768 frame; then run the calibration below once.

## No calibration

There is nothing for the user to set up.  The game window is fixed art, so the world row, the
channel grid and the two login points are shipped as the measured constants above.  The panel
therefore shows only: the **自动重连** checkbox, the world box, the 频道 box, the temporary
**测试自动重连（临时）** button and the status line - the earlier five-point calibration wizard,
the 截取游戏窗口 button and the 标定选择窗口 button were removed for exactly that reason (this
supersedes the earlier claim that the select windows are "keyboard only": the clicks are measured
now, and the keyboard is the fallback).

## Tests

`test_reconnect_worker.py` (68 tests) covers the login-page colour gate (base colour + range from
the reference, accepted page, rejected gameplay frames, the operator's own frames separating at
6 %), the login click geometry (both points inside the board, 结束游戏 outside it, the order
activation -> board -> 连接 -> world -> channel), the channel walk counts for every channel 1-60,
the list measurement and the scroll patience (`ChannelListMeasurementTests`), the closed-loop
scroll with a fake window that really scrolls one row per notch (`ChannelScrollTests` - channel 51
scrolls exactly 8 rows and double-clicks (743, 510), a swallowed wheel fails instead of guessing,
an unconfirmed double-click sends no Enter), the 1-60 input rule,
"arming the checkbox does not run the drill", and the whole sequence with a fake screen, clicker,
keyboard and clock (including "no login page colour means no key is pressed").  No test ever
touches the real screen: the fake workers inject their own `click_fn` and `wheel_fn`.
