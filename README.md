# Maple Assistant

Maple Assistant is a Windows helper for a MapleStory client. It captures the
game window, detects the minimap and player marker, follows recorded multi-layer
routes, handles ropes and recovery, performs configured attacks, watches HP/MP,
and provides alerts and quick messages. Lie detection is handled by the remote
RoiTrack service (see **测试api** below), not by a local model.

> Automation may violate a game or server's rules. Use it only where permitted.

## Reference material is read-only — never edit it

`autolie_api/` holds the vendor's own material and is **read-only**:

| file | what it is |
|---|---|
| `autolie_api/ct_lie3_remote_workflow_sample.py` | the CT client's reference workflow sample |
| `autolie_api/2.7.0.md` | the RoiTrack client protocol spec (v2.7.0) |
| `autolie_api/ip_port.txt` | the vendor's endpoints |
| `autolie_api/sample_captures/` | that sample's own mock output |

Rules:

1. **Never modify, rename, move, reformat or "fix" these files.** Not even a comment, a
   docstring, a line ending or an unused import. If something in them looks wrong or outdated,
   report it — do not touch it.
2. Write our own code in `autolie_api/intergration.py` or in a new module next to it (and keep
   the vendor sample out of the way of our own imports).
3. `test_autolie_api_reference.py` pins the SHA-256 of every reference file: any edit fails the
   test suite loudly, which is the point — this rule survives being forgotten.

The single deliberate exception is that `autolie_api/` is copied into the release package as it
is (see `build_release.ps1`); nothing inside it is altered for packaging.

The separately packaged [BOSS 追踪](boss_tracker/README.md) application is
independent and does not share Maple Assistant workers or configuration.

## Install and run

Every normal release has two ZIPs. Choose **CPU** for a machine without an
NVIDIA GPU, or **CUDA** for a machine with a working NVIDIA driver. Do not mix
the two packages in one folder.

For a release ZIP on a new Windows machine:

1. Extract the complete ZIP to a writable folder.
2. Double-click `安装.bat` and accept its one UAC prompt. The short UAC helper
   window closes immediately; only the elevated installer window remains.
   It finds or silently installs Python 3.10 and creates the package-specific
   environment: `.venv-cpu` or `.venv-cuda`. The CUDA package stops clearly
   if its NVIDIA/CUDA check fails; it never silently installs CPU tracking.
3. Double-click `启动助手.bat`.
4. Record a route, choose the patrol range, then click **开始巡逻**.

To update an existing installation, cover its files with the **same** package
type. Its launcher keeps using the existing package environment, so rerunning
`安装.bat` is unnecessary unless `.venv-cpu` / `.venv-cuda` is actually absent.

The launcher is hidden. Input is disarmed on startup; foreground monitoring,
window selection, and live game capture start only after **开始巡逻**. Manual
recording uses a short focused capture of its own. The assistant is started
through a renamed interpreter so the running process is `todo_helper.exe`
(and the game sees that name), and its window title is
`todo_helper (<version>)`.

For development:

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 assistant.py --debug-dir work/debug
```

Use `--dry-run` to inspect UI and analysis without sending game keys. See
[INSTALL.md](INSTALL.md) for installation troubleshooting.

## Recording and patrol

Recording and patrol startup are separate by design:

- Recording focuses the game and samples up to three fresh frames.
- A point is accepted only when the minimap border and yellow player diamond
  are both found.
- A successful recording updates normalized `minimap_calibration` in
  `user_config.json`.
- Patrol consumes that saved calibration; it does not require a new stable
  OpenCV-border vote every time it starts.

For each layer, record the applicable points in this order:

1. **最左** — left patrol end
2. **绳索** — rope target
3. **最右** — right patrol end

Layers are bottom to top and may be partial: no points means stand still and
attack; left/right makes a horizontal patrol; adding a rope climbs after the
configured patrol cycles. The default is two complete cycles per layer. The
highest selected route layer drops back to the first selected route layer after
its final cycle. Layer count is not fixed.

At Start Patrol, the assistant detects the character’s actual recorded floor.
It begins from that layer if it is in range; otherwise it enters return-to-route.

### Layer recognition and recovery

The visible yellow marker provides minimap X/Y. `MapStructureTracker` supplies
scroll-compensated world Y to resolve overlapping marker bands and settled
falls. The marker-Y band for a layer is:

```text
(highest recorded Y - y_tolerance * LAYER_UP_REACH_FACTOR,
 lowest recorded Y + y_tolerance / 3)
```

`LAYER_UP_REACH_FACTOR` is **0.7** (the operator's 2026-09-18 request: "narrow
down layer upper band a little bit"): a band that reaches far above a floor makes
a character still above it (on the rope, dropping in) read as standing on it.
With `y_tolerance` 0.02 that is 1.15 px above the topmost recorded point and
0.55 px below the lowest - both above the marker's own 1 px quantisation on an
82 px minimap. The recorded vertical span of a stair/bench layer stays intact,
so such a floor is drawn (and matched) as a band instead of a line.

Which floor a marker reading names is decided in this order:

1. **Bands of the route's own floors.** Only floors inside the patrol range may
   make a reading "ambiguous"; a leftover or duplicate recorded floor outside
   `route_order` is reported as `LAYER EXTRA RECORDED FLOOR:` and can no longer
   hand the decision to world Y.
2. **The nearest recorded position** of the matching floors (`_layer_y_distance`),
   not the legacy single `layer_y`. A floor whose points span a range (a
   stair/bench platform) has a base at one end of it, so the base ranks the wrong
   floor: standing exactly on layer1's recorded spot used to answer layer2.
3. **World Y** only for a genuinely ambiguous reading (two route bands overlap),
   and only inside a calibrated world band. The `LAYER transition candidate:`
   and `LAYER CHANGED:` lines now print the evidence (`marker_y`, the floors it
   matched, `world_y`, confidence), with `LAYER world override:` for the case
   where world Y overrules the marker.

Recording frames matter as much as the bands: each point stores
`coordinate_v2` (diamond-relative) plus its `recorded_layout`. The canvas
sub-detection can flip between "the whole analysis box" and "the detected
drawable area" inside one recording, and re-projecting through the live canvas
then moved correct points by ~10 px (layer1's stance to 0.8049, layer2's to
0.5915/0.7195), which made a marker on layer1 match layer2 and drew layer2 as a
12 px band. The projection therefore keeps the saved point whenever the
**analysis box** and the **diamond size** (a real minimap zoom) still match, and
`LAYER RECORDING:` warns when one floor's points were saved in different canvas
frames.

Movement owns directional keys and paired Z pickup. It releases and re-arms
these holds around focus dips, recovery, and route changes to prevent stale
directions freezing the character. Rope navigation is minimap-based; YOLO is
not required.

#### Loop descent to the first route layer

After the final layer's cycles the character Alt+Down drops back to the loop's
first floor. **The descent owns the machine until it gets there**: Alt+Down drops
one platform per chord, so the marker passes through the floors in between (and
the character even stands on the next floor up between chords), which the normal
layer tracker used to confirm as `LAYER CHANGED: layer3 -> layer2; restarting
layer2 patrol` - the descent stopped one floor short and the loop never returned
to its first floor. While `_descending_to_first` is set the tracker keeps the
route and logs

```text
DROP TO FIRST: the marker is on layer2 during the planned descent; keeping the route on layer3
until layer1 is reached (the descent owns the floors in between, and no
knock-down/return logic may interrupt it)
```

For the same reason the descent **blocks the knock-down and return-to-route
recoveries** (the operator: "the back to base patrol layer should block the hit
down by monster function, don't trigger back to patrol route"): the layer resync,
`_track_fall`, `_verify_out_of_range_floor` and the self-rescue all stand down
while it runs, so two recoveries never fight over the same character. The descent
is bounded - after `DROP_TO_FIRST_MAX_SECONDS` (10 s) without reaching the first
floor the state is handed back - so suppressing them cannot deadlock. Arrival
itself is the descent's own test (`_final_drop_arrived`), and it requires the
marker to actually be there: a reading that matches **no** floor and is not
at/below the first floor's band is mid-air, so the descent keeps dropping (his
14:38 log declared arrival at marker_y 0.481707 while the character was still
falling and then landed on layer2, because the world fallback confirmed it from a
world reading that sat inside layer1's recorded world band mid-air). The world
signal is consulted only for a genuine tie between floors. The loop restart
initializes the same state as any fresh Start Patrol - fall tracking, return
state, resync candidate, rope lock, events.

#### Endpoint arrival and turning

Walking a recorded 最左/最右 point ends when the marker passes that X within
an arrival band. **Both endpoints use the same band** - `horizontal_tolerance`,
or `horizontal_tolerance_diamonds` scaled by the measured diamond width when
that setting is present. The former quarter-width band on the left endpoint is
gone: a saved left-most point that renders just outside the walkable platform
could never satisfy it, so the character walked into the edge until the
self-rescue restarted the whole patrol.

Two bounded fallbacks keep an unreachable endpoint from freezing the patrol,
both reusing the existing `_force_advance_phase` path (which turns to the next
recorded phase and requires the new direction to show real movement before the
following endpoint may advance):

- **Near but unreachable** - the marker stays within four times the band for
  15 frames: `endpoint unreachable on layer2: left at distance=0.004000
  outside the 0.002500 arrival band for 15 frames; treating it as reached`.
- **No progress** - the distance to the target stops closing for 30 frames
  (attack pauses excluded): `endpoint left unreachable on layer2: no progress
  toward x=0.290476 for 30 frames (distance=0.050000); treating it as
  reached`. A real walk always closes the distance, so a long approach is
  never cut short.

Every route-state change is logged as `route state: <label> (target_x=...
climbing=... patrol_enabled=... return_mode=... layer=...)`, with labels such
as `layer2.left-most`, `layer2.right-most`, `layer2.rope`, `patrol-paused`,
`stand-still`, `waiting-marker`, `drop-to-route` and `return-climb-waiting`.
A character that stands still or walks into the edge therefore always has a
named cause in the log.

## Attacks, timed drugs, buffs, and the motion arbiter

### Attack modes

- **固定攻击** taps the chosen key every `base + random gap` seconds and shows
  the effective range in the UI.
- **跳跃攻击** sends Alt followed by the attack key in each beat.
- **随机跳跃** optionally queues an independent Alt jump.
- **台阶跳** watches for seven frozen minimap position samples at a patrol
  boundary and issues one forward Alt jump; it then skips five detector
  samples before it can qualify again.
- **小碎步** optionally makes an atomic left/right or right/left 300 ms pair
  with a 100 ms neutral gap.

`MotionArbiter` serializes action motions against attack. It permits queued
motions only during normal left/right patrol or movement toward a rope, waits
briefly after an attack, and lets the movement worker cleanly pause/resume its
normal walk hold.

`StairJumpWorker` is intentionally separate from `MotionArbiter`. When 台阶跳
qualifies, it stops *new* attacks and waits only for the tail of an in-flight
attack. The normal patrol Left/Right hold remains active throughout that wait;
the worker adds one short Alt tap rather than releasing and restarting the
walk. This prevents the post-attack pause that could leave the character still
before a stair.

A Left/Right direction switch reserves `direction_transition_event` for the
whole handoff, so a fixed attack can never land between Left-up and Right-down
(or the reverse). While it is reserved the attack worker logs
`attack skipped: patrol direction handoff is active`. The reservation is
released on **every** exit from the handoff (including a raising key send), is
force-cleared by the hold-manager watchdog after about 1.5 s, and is cleared on
every patrol (re)start. A `SendInput` failure such as
`[Errno 87] SendInput injected 0/1 events` used to escape the handoff block
and leak the reservation, which skipped every attack until the assistant was
restarted.

Buff taps are motion-arbiter actions: they release the normal walk hold for the
tap and the next patrol frame re-arms it, so a short buff interval (for example
a 2.5 s buff) visibly interrupts the patrol walk at every tap while still
patrolling correctly. Buff keys are configured per machine (`buff1_key`,
`buff2_key`, ...); a human pressing those same in-game buff keys is a foreign
event the assistant cannot see.

### HP/MP and timed rows

HP and MP potions are urgent direct actions. They use bar ratios rather than
OCR numbers, retry blocked sends, and verify that a bar responds.

The detection region is the bottom-centre info bar: a **425x32** capture at
the 1080x768 preset and **538x40** at the shared 1366x768 / 1920x1080 preset,
bottom-anchored with its centre tracking the info bar (the updated UI draws
it right of the client centre).  HP (red), MP (blue) and EXP (yellow) occupy
three side-by-side zones of one vertical band, and with the current UI each
bar is **130 px wide** at the 1080x768 preset.  Each bar is measured only
inside its own zone and the full-bar length self-calibrates, so the three can
never be mixed.

The timed rows in **药品** intentionally have different behavior:

| UI row | Role | Execution |
|---|---|---|
| 宠物食品 | timed drug | direct key tap; never waits for or interrupts movement |
| 增益 1 | timed action buff | queued by `MotionArbiter` |
| 增益 2 | timed action buff | queued by `MotionArbiter` |

When 增益 1 or 增益 2 becomes due, it registers one queue event even if the
character is climbing or transitioning. The event waits for normal left/right
patrol or rope approach, releases the current walk hold, sends the key, waits
through its action window, and then returns movement control. Its next
countdown starts only after this full execution succeeds. A failed send leaves
the timer due for a retry; duplicate waiting requests collapse to one per key.

## UI and hotkeys

The compact UI has two top-aligned columns:

- Left: **图层校准与巡逻**, **攻击模式**, **快捷消息**.
- Right: **附加功能**, **药品**, **运行日志**.

Initial height is fitted to the taller column so stale saved geometry does not
leave a large empty lower area. The custom title bar keeps native resize,
minimize, and maximize behavior. Dragging shows a thin outline and uses a
native final move, avoiding Tk redraw flashes.

**附加功能** holds the safety and utility rows: 测谎 / 掉线 / 循环 alerts and
their 声音 / 闪烁 / 消息 outputs, then one compact line with the two selections -
**自动过测谎** (the api pass runs by itself when the game's lie window appears) and
**自动重连** (armed by 掉线, confirmed by the login page's base colour, then a
measured login -> world -> channel sequence) with the world and 频道 they apply to -
below them **测试自动重连（临时）** and the **测试api** drill on one row, and at the
bottom the shared hint area where 自动过测谎 and 自动重连 report their progress.
Both selections are saved in `user_config.json` (and written as soon as the widget
loses focus).  See below for the RoiTrack service.

**运行日志** shows significant events. Its controls copy the in-memory log,
copy user configuration, and export `user_config.json` to Desktop. The `↻`
title-bar button searches Desktop locations for a newer release, applies it,
preserves an unchanged tagged user configuration, and restarts the assistant.
Failures are explained in the running log.

Physical Ctrl hotkeys are stored in [hotkey.json](hotkey.json):

| Keys | Action |
|---|---|
| Ctrl+1 … Ctrl+0 | send quick message 1 … 10 |
| Ctrl+Left / Ctrl+Up / Ctrl+Right | record left / rope / right |
| Ctrl+Down | select next layer |
| Ctrl+Home | select next patrol start layer |
| Ctrl+[ / Ctrl+] | decrease / increase fixed-attack base interval |
| Ctrl+Insert / Ctrl+Delete | add / delete highest layer |
| Ctrl+` | start or stop patrol |
| Ctrl+Z | toggle manual rapid pickup while patrol is stopped |

Each discrete hotkey has a cooldown. Recording hotkeys refuse route changes
during patrol. Chords Windows will not let the assistant register natively
(for example Ctrl+` when another application already owns it) are delivered
by the low-level hook instead, so they keep working; the global hotkeys are
only claimed by the interactive UI - a headless `--no-ui` run never grabs
them. The hook ignores assistant-generated input and does not steal
ordinary game typing. Every quick-message row is left-aligned and displays its
current **Ctrl+1 … Ctrl+0** index; deleting a row immediately shifts the later
indices. Buttons short-click to copy, double-click to focus the game and send
Enter → Ctrl+V → Enter, and long-press to edit.

### Trade hotkeys

Trade is available in the normal Maple Assistant package and is deliberately
kept outside the patrol UI and help list:

- **Ctrl+Q** starts a trade invitation, or cancels the pending invitation.
  Place the real cursor over the target player in the focused game first. The
  assistant performs the right-click and Start Trade clicks with virtual mouse
  input, waits up to one second for the trade dialog, then waits for the other
  player to join. Once joined, it focuses the game, sends quick message 1,
  and confirms the trade.
- **Ctrl+W** accepts an incoming invitation, confirms the trade dialog, then
  sends quick message 1.
- **Esc** cancels only an active Ctrl+Q invitation. It has no special effect
  when no invitation workflow is running.

Trade coordinates use the client size: the 1366×768 and 1920×1080 HUD layout
shares its fixed coordinates, while the 1075×768 layout scales them uniformly.
The presence sample is intentionally invisible; trade does not create a Tk
overlay or any visible target/range drawing.

## 自动重连 (automatic reconnect)

Armed by the **自动重连** checkbox and triggered by the 掉线 event (or right now by the temporary
**测试自动重连** button).  The full design, with every measured number and every field log that shaped
it, is in `RECONNECT_README.md`; this is the shipped behaviour.

**It is not a patrol workflow.**  The 掉线 detector lives in `CharacterWorker`, fed by the same capture
as everything else, and that capture is idle before Start Patrol - so the reconnect used to be
impossible while parked.  `ParkedWatchCapture` (`capture_worker.py`) closes that hole: while **掉线** is
ticked and the game window is in front, it grabs the window itself and feeds the character queue, so a
掉线 is noticed and the reconnect can run with no patrol started.  The same watch feeds the lie detector
for 自动过测谎.  `掉线` is the detector the reconnect waits for - arming 自动重连 without it logs a
WARNING instead of staying silent.

**The sequence** (client pixels, 1366x768 reference space - at another client width the LOGIN screens keep
their size and are centred while the WINDOW screens scale, so every point goes through one of the two
mapping families in `reconnect_worker.py`):

1. bring the game forward, then **click (50, 50)** - the game ignores keys until it has seen a click;
2. **click the login board**, then **click 连接**; 结束游戏 lies outside the clamped box and can never be
   clicked; if the 连接 click does not take, **Enter** is tried instead;
3. dismiss the 掉线提示 by clicking its close button and confirming with **Enter** (both verified, never
   sent blind), and only continue once the prompt is gone;
4. **click the chosen world row** at `(498 + (w-1)*104, 163)` - the five worlds are one horizontal
   line - and only confirm with Enter **after the highlight moved**;
5. **click channel 1**, **scroll the calculated number of rows** (the list is paged 1-20 / 21-40 /
   41-60 with four channels per row and five rows visible, so channel 51 needs `13 - 5 = 8` notches,
   one notch per row), then **double-click the calculated cell** (a single click and Enter land on
   channel 1 - the field failure that shaped this step);
6. **Enter**, 2 s, **Enter** - the login starts.

Clicks go out through `SendInput`; the legacy `mouse_event` path is only a fallback, because this game
ignores it.

**What gates what.**  The login page is recognised by its base colour (`screenshots/login_page_target.jpg`)
AND by two small crops of the page itself; the world and channel pages by two crops each
(`page_*_target.jpg`, threshold 0.60, a page's own score being the minimum over its crops).  Those
measurements are **evidence**: they are logged (including every signature's score and a saved frame when
they miss) and they never abort a step whose clicks are arithmetic.  The one hard rule is that **Enter is
never sent to confirm a selection that did not move** - that is how the wrong world used to be logged in.

**After a successful login** the assistant re-detects the layer and prepares the patrol again
(`restart_patrol_after_reconnect`), but only when the operator's own last choice was **开始巡逻**: a
disconnect stops a running patrol by itself without touching that choice, so the v1.0.31 case
("character logined but patrol did not start") still restarts while a parked assistant stays parked and
says so in the panel.

**Ownership and safety.**  A click is sent only when the window at that point belongs to the game's
family (the game window, its process, its executable, its window class, its title, or its own login UI
window `igwUserLoginDialog`), the point is inside the game's **client** rectangle, and the window is not
one of ours - the panel is first pushed to the bottom of the Z order and the point re-checked.  Anything
else is refused and named in the log together with the windows over that point.  While a run is active
the reconnect owns the keyboard (`begin_exclusive`), Alt/Ctrl/Shift are force-released before every key,
the automation switch is cleared (the focus gate honours `reconnect_active`, so patrol cannot re-arm
itself), and after a **failed** login the live input stays disarmed - patrol must never run on the login
page.

### 自动过测谎 (the api pass on a lie window)

**自动过测谎** is a selection, not a button: while it is armed, the lie detector's own event (the same
one that plays the 测谎 alert) starts one api pass, and the pass follows the **same workflow as the
video drill** (`api_lie_video.py`) because that is the workflow the service was measured with:

1. **connect first** - probe, handshake and session while the lie window is still coming up;
2. **wait `AWAIT_SECOND_WINDOW_SEC` (3 s)** - the window's HUD square is there the moment it opens, but
   its real content needs ~3 s (measured on the operator's clip: the first 16 frames were one frozen
   image and the service answered `abandon_frame_hold` for every one of them).  Nothing is built, sent
   or billed during the wait, and the cursor is already claimed;
3. **feed** - capture, upload the ROI, take the returned `(x, y)` and **execute** it with the vendor
   `MouseAimController`, drawing the crosshair overlay on the game.

The wait belongs to the pass length: `AUTO_LIE_PASS_SECONDS` (8 s) is the whole pass, i.e. 3 s wait +
~5 s of feeding at 5 fps.  Only one pass runs at a time (`AUTO_LIE_MIN_PASS_GAP_SECONDS` 10 s between
passes, one pass per lie window, a pending event waits at most `AUTO_LIE_PENDING_MAX_SECONDS` 90 s), the
patrol is paused for the pass only if it was running and resumed afterwards, and the panel drains
progress on Tk's own thread so the detector's thread never touches Tk.  The manual **测试api** drill (a
chosen video, 5 fps) stays as the way to test the service without a real lie window.

**Working without Start Patrol:** the lie detector is fed by the parked watch described above as soon as
**测谎** is ticked, so a lie window is caught - and answered - while the patrol is stopped.  `测谎` is
what enables the detector; arming 自动过测谎 without it logs a WARNING.


## Optional alerts

Alert sources are **掉线**, **测谎**, and **循环**. Output choices are independent:
**声音** plays `sound/dingdong.mp3`, **闪烁** flashes red twice, and **消息**
sends Telegram with machine name, event type, and time.

- 掉线 reuses the yellow marker result and alerts after `DISCONNECT_ALERT_FRAMES` consecutive missing
  frames - **40** since 2026-09-18 (it was 120), i.e. 10 s at the default 0.25 s capture cadence.  The
  threshold is a frame count, which is why the parked watch feeds this queue at the normal cadence.
- 测谎 scans the shared capture once per second for a resolution-scaled white
  square and saves no screenshots.
- 循环 is an independent draggable countdown; it does not depend on patrol.

测谎 and 掉线 are what feed the two event workflows (自动过测谎 / 自动重连).  Both are fed while parked by
`ParkedWatchCapture`, which stands down the moment the normal patrol capture runs (no double capture) and
never foregrounds the game: the 掉线 feed needs the game window in front (a frame of the game covered by
this panel would look like "marker missing" and fire a false 掉线).
- Its remaining deadline is saved to ignored `timer.json` when the UI closes.
  A future, unexpired deadline is restored at the next start; expired timers
  remain unselected.
- Telegram failures only change UI/log status and never stop the assistant.

Sound playback is bounded and cannot wedge a worker. `countdown_worker.play_mp3`
(shared by the countdown, the 掉线/测谎 alerts, trade, and action feedback) does
**not** use MCI's `play ... wait`; it starts the clip, polls
`status <alias> mode` every 50 ms against a 30 s budget, always closes the MCI
alias, and never raises (an optional `stop_event` aborts early). A clip that
has to be cut off logs `COUNTDOWN sound exceeded ...; stopping it and pausing
sound for 30s` and puts the audio device in a 30 s cooldown, during which
further sounds log `COUNTDOWN sound skipped: the audio device is unavailable
(previous clip had to be cut off)` instead of piling more stuck threads onto a
broken device. The 掉线 and 测谎 alerts play on daemon threads
(`run_sound_async`; thread names `disconnect-alert-sound` and
`lie-alert-sound`), so a dead audio device can never freeze detection.
`wait` blocks the calling thread until the device reports the clip finished;
when the audio endpoint disappears that never happens, which was observed as a
permanently stuck `action-sound-success` thread inside `mciSendStringW` with a
leaked alias.

## Lie detection over the RoiTrack API (测试api)

Lie detection is done by the **remote RoiTrack service**, not on this machine. The local
fade-target tracker (自动过测谎 / Cutie) was removed with its engine and its 437 MB of offline
weights, so no torch/Cutie install is needed any more.

The **附加功能** panel row has exactly one control: the **测试api** button. There is no 时长 box
and no 密钥 button on purpose - the run length is the measured ~30 s and the product key travels
with the application.

Pressing it:

1. asks for a **video file** (any `mp4/avi/mkv/mov/wmv/flv/m4v`; the picker remembers the last
   folder),
2. plays that clip in its **own window, focused and always on top**, at the API's **5 fps**,
3. uploads each frame's lie ROI to the service and moves the real cursor onto the answer -
   **only inside the picture rectangle**, never over the game, and **no click is ever sent**,
4. closes everything when the clip ends (round_end, socket, aim, cursor restored, window closed).

The order matters and is fixed:

| Step | What happens |
|---|---|
| t0 | the drill **connects at once** (probe -> chosen port -> `handshake`): the session is ready before there is anything to send |
| t0 -> t0+3 s | `AWAIT_SECOND_WINDOW` (3 s): the clip plays and is displayed, but **nothing is fed** and nothing is billed |
| t0+3 s | feeding starts - 372x248 BGR JPEG (q90) -> base64 -> `frame`; `frame_id` starts at 1 for the round and never has gaps |
| video ends / 30 s | teardown: `round_end`, socket close, aim stop, cursor restore, log close |

The whole run is one 时长 (~30 s, the wait included), so the feeding part is ~27 s / 135 frames.

The wait exists because of a measurement on the operator's own clip: its first 16 frames are one
frozen image and the service answered `abandon_frame_hold` for every one of them - the real lie
window content only appeared ~3.2 s in. Feeding before the second window is on screen therefore
wastes the round. Because the session is already open during the wait, the wait is **clamped to
8 s**: 2.7.0 §4.1 drops a session that goes 10 s (`idle_no_frame_sec`) without a valid frame, and
that is the one timing rule that could make the service close the connection.

Other protocol facts the drill honours (see `autolie_api/2.7.0.md`):

- the service does **reset the socket mid-round** sometimes (observed once: `WinError 10054` after
  69 frames, with frames being answered in 20-29 ms right up to that moment, and no close frame).
  The drill treats it as normal: it opens a new session, restarts `frame_id` at 1 and keeps going,
  counting `rounds` / `reconnects` in the summary. A closed socket is *not* mistaken for "no answer"
  (that bug cost two frames and a 1.5 s stall per reset),
- `frame_standard` (3-7, ~fps) is declared as 5 and explicitly does **not** limit frames per round,
- the frame must stay under `max_frame_bytes` / `max_frame_b64_chars`; JPEG q90 measures 24-39 KB on
  real lie frames, well inside the cap.

The ROI is the same measured geometry as before, now in `lie_geometry.py` (a pure-geometry module
with no capture and no model): the lie popup is a centred 767x598 game-UI preset, identical on the
1366x768 and 1920x1080 clients and scaled by width below that; `autolie_api/intergration.py` insets
it by 11/11/33/69 to get the precise ROI, which is exactly **(310, 118, 745, 496)** at 1366x768.
The crop is the service's own lie3main space, so `x_main`/`y_main` maps 1:1 onto it.

The key is **not configured in the UI**: `autolie_api/key_secret.txt` ships inside the package and
`LIE_PRODUCT_KEY` overrides it (use the environment variable if a later release must move the key
elsewhere). With neither, the drill runs against a **local mimic** backend (`autolie_api/mock_backend.py`)
so the button can be tried without spending a round. The panel line always names which backend and
which key source will be used - never the key itself.

Every run writes `work/api_test/api_lie_video_<stamp>/`:

| File | Content |
|---|---|
| `connection.log` | every probe, the chosen port, `handshake_ack` (auth/quota/config), `feeding starts`, reconnects, `round_end` |
| `detection.jsonl` | one JSON object per frame: bytes sent, the answer, both coordinate spaces, where it mapped, cursor before/after |
| `summary.json` | outcome, `ended_because`, ticks, await/feeding seconds, sent/answered/holds/misses, `rounds`/`reconnects`, timings, accuracy |
| `frames/*.jpg` | annotated frames (ROI box, answer cross, crop-local dot, HUD) |

The panel prints that folder when a run ends; a failure is also appended to root `error.log`.

Related modules that stay: `lie_detector_worker.py` (the #c9ced0 white-square detector feeding the
**测谎** alarm and the recorder), `lie_screenshot_recorder.py` (alert-triggered full-client
recording) and `lie_video_tools.compose_frames_to_video` (a recording folder -> one mp4 - that is
how the test clips are produced). Its old in-game replay through the local tracker is gone with the
tracker. `api_lie_test.py` (the older single-frame screen drill) also has no panel button any more;
it remains as an offline tool for `work/` scripts and the tests.

### Why the local pass is gone

`target_tracker/` now contains only `mouse_aim_controller.py` (the stdlib-only aim controller the
video drill uses, imported after adding that folder to `sys.path`) plus its README. Deleted with
the local pass: `realtime_fade_tracker`, `adaptive_target_tracker.py`, `roi_video_tracker.py`,
`video_player_tracker.py`, `offline_bundle/` (Cutie + ResNet weights, wheelhouse, python/vc_redist
installers), `tracking_results/`, the tracker launchers/docs, `auto_lie_worker.py`,
`lie_demo_player.py`, `auto_lie_overlay.py` and their tests. `install.ps1` no longer installs
torch/Cutie/VC++ redist for a tracker (`$InstallLocalTracker = $false`, the same pattern as the
already-disabled YOLO dependency block), which takes the CPU/CUDA package from ~379 MB to ~62 MB.

### Installation behaviour (see `install.ps1`)

- pip mirrors fall back TUNA - Aliyun - Tencent - official PyPI; the venv is self-healed when pip is
  missing (ensurepip, recreate, get-pip.py).
- The package environment (`.venv-cpu` / `.venv-cuda`) still carries the CPU/CUDA marker in
  `release_variant.json` and the matching launchers; only the local tracker step is switched off.
- The initial UAC helper closes at once. The elevated installer window remains open only to show
  progress and the final success/failure result.
- On startup the assistant clears every previous `*.log` (single-instance check happens first), and
  all levels (INFO+) are written to the single `error.log` file; `work/assistant.log` no longer
  exists.

### Diagnostics

- The **运行日志** panel gained an alert-triangle (❗) icon button that copies the whole root
  `error.log` to the clipboard.
- `diagnose.bat` prints OS build, CPU, VC DLL presence, registry version and Python - the standard
  first response for missing-runtime reports.

## Configuration and updates

| File | Ownership | Purpose |
|---|---|---|
| `user_config.json` | user | route, minimap calibration, UI/drug/attack/alert/Telegram settings |
| `system_config.json` | release | internal movement and rope calibration defaults |
| `hotkey.json` | release | default Ctrl bindings |
| `timer.json` | local runtime | remaining circular-alert deadline; never packaged or committed |

`user_config.json` has a `user_config_updated_at` tag. The built-in updater
compares this before replacing a Desktop configuration: matching nonempty tags
preserve the installed user file. Do not overwrite user configuration during
ordinary development or updates. Legacy `config.json` and former split JSON
files are migration inputs only.

Use **导入配置** in **运行日志** to select and atomically import a saved
`user_config.json`; it takes effect after restart. **导出配置** overwrites the
Desktop export and reports both import and export results in the running log.
Fatal startup and worker failures are additionally recorded in root-level
`error.log` for troubleshooting.

## YOLO status

YOLO monster detection is temporarily disabled because the model is not yet
reliable. Patrol, rope logic, fixed attack, potions, buffs, and alerts work
without it. To restore it after obtaining a better `weights/best.pt`, re-enable
`_SHOW_YOLO_PANEL` and `_YOLO_MONSTER_DETECTION_ENABLED` in `ui_worker.py`,
restore the documented installer dependency block, then update both handoff
documents before publishing.

## Release, testing, and maintenance

Every behavior change requires a new numbered ZIP:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

Testing is not a default ritual. Run only the smallest targeted check that is
necessary to validate the code being changed; do not run duplicate or broad
test suites merely as a routine step. **Never re-run a suite that already
passed in the same session, and never re-run tests for a second iteration of
the same simple change** (a constant, a timing, a click order, a log line): the
operator's rule is "don't do redundant unit testing unless I tell you to".
When a change is simple, ship it and let the field run be the test. When a
targeted check is genuinely needed, run the affected test cases only - not the
whole file, and never the whole discovery set.

Documentation-only edits do not require tests or a release ZIP.
`release_now.ps1` advances the semantic `VERSION` (`X.Y.Z`, patch by default),
builds the single `release/MapleAssistant` package, and produces
`release/MapleAssistant-<version>.zip`, removing the previous ZIP. There is no
CPU/CUDA split any more: one package for both, with the YOLO weights optional.

### Machine-specific diagnosis

When an issue is reported from another machine, diagnose it only from the
logs, screenshots, settings, and files explicitly supplied for that machine.
Do not inspect or use this development machine's live settings, logs, or
running state as evidence unless the user explicitly says the affected run is
local.

`release_no_trade.ps1` is a separate publisher for the special
`MapleAssistant-vnt-0001.zip` package. It stages a copy of the current normal
release, strips only trade wiring from that copy, verifies it, and zips it.
It never changes the normal source tree; `main` stays the trade-enabled
version.

### Movement stall watchdog

When patrol input is armed, `MovementWorker` runs a watchdog (5 s poll). If no
frame has been consumed for 20 s it logs one ERROR block:

```text
movement worker silent for 20.0s while patrol input is armed (thread alive=True
patrol_enabled=True return_mode=None route_phase=right route_state=layer2.left-most
walk_hold=left handoff_reserved=True) - dumping thread stacks
```

followed by the stalled worker's **own** stack first (labelled `thread <name>
(the stalled movement worker) stack:`) and then every thread's stack.

How to read it: a stack sitting in `frame_queue.get(...)` means no frames were
produced, so the capture worker - not movement - is the suspect; a stack inside
a lock or a Win32 call (`key_down`, `_send_scan_code`, `SendInput`) means the
movement thread itself is wedged. Healthy workers idle in queue/event waits
with a timeout (`frame_queue.get(timeout=0.2)`, `stop_event.wait(...)`); those
are normal and are not evidence of a fault. The header is the important part:
`thread alive` separates a dead worker from a blocked one, and
`route_state` / `route_phase` / `return_mode` / `handoff_reserved` name the
state the worker was in.

## Important files

| File | Responsibility |
|---|---|
| `assistant.py` | application wiring and lifecycle |
| `api_lie_video.py` | 测试api: play a chosen video at 5 fps in its own focused window, upload the ROI, aim inside the picture, log the run |
| `api_lie_test.py` | offline single-frame API drill (no panel button; used by `work/` scripts and the tests) |
| `autolie_api/` | our RoiTrack client (transport, endpoints, key store, per-run logs, local mimic) plus the vendor's read-only reference material |
| `lie_geometry.py` | measured lie-popup geometry (popup preset, feed insets) - pure geometry, no capture, no model |
| `lie_detector_worker.py` | #c9ced0 white-square detector behind the 测谎 alarm and the recorder |
| `lie_screenshot_recorder.py` | alert-triggered full-client diagnostic recorder; composes its run into one mp4 |
| `lie_video_tools.py` | frames -> mp4 (H.264 via ffmpeg when available; decoupled, never moves the mouse) |
| `image_io.py` | JPG-only image policy (screenshot/recording/reference quality, frame listing) |
| `capture_worker.py` | full-client capture + `FrameBus`, and `ParkedWatchCapture`/`WatchFeed` - the watch that keeps 测谎 and 掉线 (and therefore 自动过测谎 / 自动重连) alive with no Start Patrol |
| `ui_worker.py` | Tk UI, recording, settings, update/export actions |
| `movement_worker.py` | patrol, rope, fall/recovery, directional ownership, endpoint arrival bounds, stall watchdog |
| `motion_arbiter.py` | serialized random-jump/buff/small-step actions |
| `stair_jump_worker.py` | one-at-a-time stair Alt tap that preserves patrol walking |
| `status_worker.py` | HP/MP bars, potions, timed drug/buff scheduling, shared key sender |
| `attack_worker.py`, `random_jump_worker.py` | attack and optional jump timing |
| `quick_pickup_worker.py` | Ctrl+Z manual-only rapid pickup when patrol is stopped |
| `reconnect_worker.py` | 自动重连: login-page colour check, then the measured login/world/channel sequence (page verifier, calculated clicks, nothing to calibrate) |
| `timer_state.py` | atomic persistence for the independent circular-alert deadline |
| `trade_worker.py` | Ctrl+Q/Ctrl+W virtual-input trade flows and invisible presence check |
| `patrol_control.py` | route model and persistence |
| `minimap_detector.py`, `marker_detector.py`, `map_structure_tracker.py` | minimap geometry, marker detection, world-Y tracking |
| `config_store.py`, `update_manager.py` | configuration ownership, import/export, and self-update |
| `error.log` | rotating fatal-error log, generated beside the application |
| `diagnose.bat` | one-shot environment diagnostic (OS/CPU/VC DLLs/Python) for missing-runtime reports |
| `target_tracker/` | `mouse_aim_controller.py` only: the aim controller the video drill uses to move the real cursor inside the picture |
| `detect_video/` | the operator's own lie-event clips and frames, handy as 测试api inputs |
| `ARCHITECTURE.md` | detailed worker wiring and complete repository inventory |

## Future-agent checklist

1. Read this file and [ARCHITECTURE.md](ARCHITECTURE.md) before changing code.
2. Inspect `git status --short`; preserve user-owned configuration and unrelated edits.
3. For movement/input bugs, follow the logged state transition and ownership path before changing thresholds.
4. Keep Left/Right/Up/Down serialized; Z and Alt have their own valid movement roles.
5. Build a new release ZIP for every behavior change; never build one for
   documentation-only work.
6. Run only a necessary targeted test, not redundant checks. Update docs and
   commit only when the user requests an update or a handoff is needed.

### Handoff state

- `VERSION` is **1.0.53** and the single package `release/MapleAssistant-1.0.53.zip` is the current
  distributable (the previous ZIP is removed automatically). `release_now.ps1 -SkipTests` is the normal
  release command; one release per behaviour change, none for documentation-only edits.
- The 1.0.18 - 1.0.48 line (all shipped during this session, each one behaviour change):
  - **1080 clients**: the reconnect's screens live in two resolution families - LOGIN keeps its size and
    is centred (`x + (w-1366)/2`), WINDOW scales with the width - and every preset was re-measured from
    the operator's 1080 screenshots (login page 0.994, world 0.980, channel 0.995 template scores).
    Clicks go through `SendInput`, the 掉线提示 is closed by its anchor + Enter (verified), and the
    channel cell needs a double-click.
  - **Patrol resumes after a reconnect without 开始巡逻** (`on_patrol_restart`), and only when the
    operator's own last choice was 开始巡逻.
  - **自动过测谎 executes and draws the answer** (vendor `MouseAimController` + crosshair overlay) - the
    v1.0.26 "it doesn't take control" report.
  - **自动过测谎 and 自动重连 work with no Start Patrol** (`ParkedWatchCapture`: the lie detector and the
    掉线 detector are fed while the shared capture is parked; the pass runs the video drill's
    connect -> 3 s second window -> push workflow).
  - **Layer recognition**: `LAYER_UP_REACH_FACTOR` 0.7, the landing floor cap (a fall can never be
    resolved to a floor the marker draws the character above), the route-scoped marker-ambiguity test,
    ranking by the nearest recorded position, the projection-frame fix, and the descent guard
    (`DROP_TO_FIRST_MAX_SECONDS`). New evidence lines: `LAYER WORLD BAND` / `LAYER WORLD BAND OVERLAP`,
    `LAYER EXTRA RECORDED FLOOR`, `LAYER RECORDING`, and the `LAYER transition candidate` / `LAYER CHANGED`
    / `LAYER world override` lines now print `marker_y`, the matched floors, `world_y` and confidence.
  - **1.0.49 / 1.0.50**: a reading that matches no band at all now **anchors to the nearest recorded
    floor** (the operator's rule) instead of reporting "none": the bottom-floor rule when the marker is
    at/below the lowest recorded band, then `_nearest_floor_by_marker_y` (the same distance measure the
    candidate ranking uses).  That covers a stair/step on a floor whose recorded points do not span its
    whole height (layer1 renders at 0.713415 while its points were saved at 0.676829), a marker in the
    gap between two floors, and a marker above the top floor's band.  The world signal in the resync is
    additionally capped by the marker position, so it can no longer name a floor the character is
    visibly under.
  - **Band overlay**: one solid strip per floor (edge lines only when the band is >= 4 px tall) instead of
    eight gradient stripes - the operator's "the drawing on minimap tells me that it mixed".
- Open investigations (all need the operator's next field run):
  - the layer1/layer2 confusion.  1.0.45 - 1.0.47 removed three mechanisms (a stale extra recorded floor,
    the legacy-base ranking, and the canvas-frame projection); his profile still mixes canvas frames, so
    `LAYER RECORDING:` will name it and re-recording the floors is the permanent cure.
  - an in-game character freeze that only an assistant restart recovered: needs the `movement worker
    silent for ...` header plus that worker's stack (already shipped).
- Test baseline: **9 pre-existing failures** are known and unrelated (757 tests): `test_config_store` 1,
  `test_movement_worker` 6 (`centered_marker`, `descending_flag`, `off_route_marker`,
  `return_climb_finishes`, `return_climb_targets`, `return_to_first_layer`), `test_ui_worker` 2.  Per
  `AGENTS.md` unit tests are not the gate - the field run is - so `-SkipTests` is the normal mode and a
  change ships with a targeted probe (`work/probe_*.py`) instead of a suite sweep.
- Quota note: the real service bills one round per drill (`quota_left` is in every `handshake_ack` and
  frame answer). Offline checks use the mimic (`use_mimic=True` / `work/video_drill_check.py`,
  `work/probe_autolie_workflow.py`) and cost nothing - verify there before spending a round.
- `lie_active` (`assistant.py`) is still never set: the focus gate's "keep the capture open during a
  pass" exception and the 30 fps lie cadence are dormant wiring. It is harmless today (a pass captures
  its own frames, and the parked watch keeps the detector fed) but it should be wired or removed.
