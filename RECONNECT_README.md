# 自动重连 (automatic reconnect)

New in this version.  It logs the character back in after the game drops it.

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
3. presses **Enter**, waits **3 s** (the login page closes),
4. first select window: **clicks** the row of the chosen world,
5. that opens the second select window: **clicks the first channel's circle**,
6. the keyboard does the rest: **right / down** moves to the target channel, then
   **Enter**, waits **2 s**, then **Enter** again (the second one starts the login).

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
| `游戏窗口不在前台，按键被拒绝` | focus was lost between the steps |
| `按键 enter 未被接受（游戏窗口没有接管键盘）` | the sender rejected the key for another reason |
| `点击 <world>（第 N 行）失败` / `点击第一频道失败` | the click itself failed |
| `无法截取游戏窗口（…）` | the game window could not be captured for the click geometry |

## Testing it without waiting for a 掉线 (temporary)

The 附加功能 panel has a temporary **测试自动重连（临时）** button under the 自动重连
status line.  It runs exactly the sequence above, right now, and deliberately ignores the
enable checkbox - that is the point of the button: try the drill before arming the automatic
trigger.  It also **measures** the login-page colour and shows the number
(`登录页颜色检测: 同色区域 50.8%（阈值 6.0%），判定为登录页`), so the gate can be checked and
calibrated from the panel without waiting for a real 掉线 - the manual run is not blocked by
that number, while the automatic 掉线 path waits for it.  The button is disabled while a run
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

## Calibration (once, inside the app)

The world rows and the channel circles must be measured once - the built-in values are all
zeros, and with them the drill used to click the game window's top-left corner
(`clicking world 蓝蜗牛 (row 1) at client (0, 0)`, measured in the field) while still reporting
success.  The worker now **refuses to click an uncalibrated layout**
(`自动重连失败: 选择窗口未标定（世界行/频道位置未知）…`).

The panel's **标定选择窗口** button records the five points from your own mouse.  Each press
waits **3 s** (so you can move the cursor onto the game) and then stores the cursor position in
client coordinates:

| press | put the mouse on | gives |
|---|---|---|
| 1 | 世界列表**第 1 行**（蓝蜗牛） | the row's x/y |
| 2 | 世界列表**第 2 行**（蘑菇仔） | the row pitch |
| 3 | 频道列表**第 1 个频道** | the first channel's x/y |
| 4 | **第 2 个频道**（同一行右侧） | the horizontal channel pitch |
| 5 | **第 6 个频道**（下一行的第一个） | the vertical channel pitch (5 channels per row) |

You need the world list open for presses 1-2 and the channel list open for presses 3-5 (click a
world in between - the app only reads the cursor, it does not press anything).  After the fifth
press it writes `reconnect_layout.json` next to the app, uses it immediately, and reports
`标定完成：世界行距 Npx，频道间距 XxYpx，参考客户端 WxH`.  Pressing the button again after that
starts a fresh calibration (that is what a different client size needs); the file is loaded at
every start, so it is a one-time step.

The other two panel buttons: **测试自动重连（临时）** runs the whole drill now, and
**截取游戏窗口** saves one frame into the app's `screenshots/` folder (useful for diagnosis).
The command-line equivalent stays available in the repository:
`.venv\Scripts\python.exe work\reconnect_calibrate.py` (it reads the two
`screenshots/channel_select_*.jpg` files if you prefer clicking there).

### Those two bundled frames are not select windows (measured)

`screenshots/channel_select_first.jpg` / `channel_select_second.jpg` are 1366×768 but the whole
area x[500..1000] × y[150..330] is **darker than 190** with no text rows and no circles, i.e.
they do not contain the world/channel lists - so the geometry cannot be measured from them, and
the in-app calibration above is the way.  `work/select_layout_probe.py` and
`work/select_rows_zoom.py` re-run those measurements.

## Calibration from the command line (optional)

The two select windows are fixed art, so their geometry is measured once instead of being
guessed.  Run:

```bat
.venv\Scripts\python.exe work\reconnect_calibrate.py
```

and click eight points on your own screenshots:

| screenshot | clicks |
|---|---|
| `screenshots\channel_select_first.jpg` | the centre of each of the 5 world rows, in the order 蓝蜗牛, 蘑菇仔, 绿水灵, 漂漂猪, 小白兔 |
| `screenshots\channel_select_second.jpg` | the **first** channel's circle, the channel next to it in the **same row**, and the first channel of the **next row** |

It writes `reconnect_layout.json`, which the worker loads at start-up:

```json
{
  "reference_client": [1366, 768],
  "world_rows": [[x, y], [x, y], [x, y], [x, y], [x, y]],
  "first_channel_centre": [x, y],
  "channel_pitch": [dx, dy],
  "channels_per_row": 5
}
```

Everything is scaled by the live client size (`world x/y * client / 1366x768`), so the same
numbers work on any window size.  Until the file exists the worker uses built-in defaults
(shown by `py -3.10 work\reconnect_calibrate.py --from-json`), and a missing or broken file
falls back to them rather than failing.

## Tests

`test_reconnect_worker.py` covers the login-page colour gate (base colour + range from the
reference, accepted page, rejected gameplay frames, the operator's own frames separating at
6 %), the click geometry (rows, channel cells, the right/down move count for every channel
1-60), the 1-60 input rule, the calibration file round-trip, "arming the checkbox does not
run the drill", and the whole sequence with a fake screen, clicker, keyboard and clock
(including "no login page colour means no key is pressed").
