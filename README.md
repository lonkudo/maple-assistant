# Maple Assistant

> The repository also contains the separately launched and separately packaged
> [BOSS 追踪](boss_tracker/README.md) companion application. It does not share
> Maple Assistant runtime state or automation workers.

Maple Assistant is a Windows desktop automation tool for a MapleStory client.
It captures the game window, reads the minimap and HP/MP bars, follows a
recorded multi-layer patrol route, climbs ropes, recovers from falls, collects
items. Fixed-interval attack remains available; YOLO-driven monster attack is
temporarily disabled until its model has been trained more reliably.

The runtime uses a FIXED-PIXEL HUD model above ~1366px client width: the
game's minimap, map-name strip, and HP/MP/EXP bars keep the same absolute
pixel size at any window resolution in that range (1920x1080 and 1366x768
measure identically) - only the playfield viewport scales.  BELOW 1366px the
game shrinks the whole HUD (at 1024x768 everything measures ~0.75x).  All
HUD regions are therefore absolute client pixels at the HUD reference size,
scaled per frame by `hud_scale_for` in `minimap_detector.py`.  The yellow
player diamond is detected dynamically; a measured fallback region is used
as verified map geometry when OpenCV cannot close a border contour.

> Automation may violate a game or server's rules. Use it only where permitted.

## Start here

For a packaged release on a new Windows machine:

1. Extract the complete ZIP.
2. Double-click `安装.bat` and approve the single UAC prompt shown immediately.
   It finds or silently installs Python 3.10 without opening the Python setup
   wizard, creates `.venv`,
   installs the core dependencies through the Alibaba Cloud PyPI mirror and
   creates the launchers. If that mirror is unavailable, installation retries
   against official PyPI. The large YOLO dependencies are temporarily skipped.
3. Double-click `启动助手.bat`.
4. Bring the configured game window to the foreground.
5. Record or verify the patrol route in the UI, then click **Start Patrol**.

The launcher uses a hidden `wscript`/`pythonw` path: after the single UAC
confirmation it does not leave a command window behind. Game-window lookup,
foreground monitoring, and periodic game capture remain idle until **Start
Patrol** is clicked (manual recording can still request a one-off capture).

See [INSTALL.md](INSTALL.md) for installation and troubleshooting details.

For development from the repository:

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 assistant.py --debug-dir work/debug
```

Useful modes:

```powershell
# Analyze and display the UI without sending gameplay keys.
py -3.10 assistant.py --dry-run --debug-dir work/debug

# Run without the Tk debug UI.
py -3.10 assistant.py --no-ui --debug-dir work/debug

# Show command-line options.
py -3.10 assistant.py --help
```

The application starts with live input disarmed. Input is enabled only after
**Start Patrol** is clicked.

Start Patrol first selects and verifies the game in the foreground, then loads
the normalized minimap border saved by recording and scales it to the current
client resolution. It does not require the border contour to repeat at startup.
Before input is armed, every recorded marker-Y band is drawn directly over the
live minimap as a distinct translucent vertical colour-gradient rectangle.
Overlapping areas visibly mix/darken, and the log prints each layer's exact Y
bounds. The overlay closes before a clean frame is published and patrol keys
are enabled, so it cannot alter runtime marker detection.
The focused startup capture must detect the yellow marker inside a recorded
layer before input is armed. That detected floor is handed directly to the
movement worker: an in-range floor starts from its first recorded patrol action
at cycle 1, while an out-of-range floor immediately starts return-to-route.
Every Start Patrol clears stale climb, drop, fall, stair, and return state left
by an earlier Stop Patrol, so starting on any upper floor cannot inherit a
lower-floor rope recovery. Layer count is not fixed: for example, a four-floor
map with patrol range layer3 through layer4 starts either in-range floor
directly, climbs through layer1/layer2 when starting below the range, and drops
from layer4 back to layer3 after the final patrol cycles.
For a legacy route without saved calibration, startup can discover an OpenCV
border only when its region independently contains the yellow character
diamond; the broad fallback search region is never used as map geometry.
Keyboard input is armed only after preparation succeeds, and window checking
remains idle before Start Patrol.

## What the assistant does

- `CaptureWorker` captures the client and publishes latest-only frames.
- `MinimapDetector`, `marker_detector.py`, and `MapStructureTracker` locate the
  minimap, yellow/red diamonds, and scroll-compensated map position.
- `MovementWorker` owns patrol movement, endpoint sequencing, stair jumps,
  rope approach/climbing, fall recovery, route return, and self-rescue.
- `StatusWorker` reads HP/MP/EXP and sends potions directly (urgent,
  bar-verified path); the periodic buff rows (宠物食品/增益1/增益2) are
  queued to the motion arbiter instead of tapped raw.
- `AttackWorker` runs the fixed-rate attack (固定攻击) or the 跳跃攻击 bundle
  (jump first, then the attack key 300ms later); both defer while the
  motion arbiter is busy and report their tap for the attack-motion grace.
- `RandomJumpWorker` optionally queues an Alt jump on its own
  `base + random gap` timer (base minimum 1.0s) and observes the same
  patrol/climb input gates as fixed attack.
- `MotionArbiter` (motion_arbiter.py) is the single executor for jump and
  buff motion keys: a FIFO queue (duplicates collapse), one tap at a time,
  a 0.9s lock after a jump and 0.6s after a buff, attack suppressed while
  events are queued/executing, and a 0.3s grace after every attack tap so
  attack motion cannot swallow the next event.
- `HotkeyWorker` listens for physical and foreign-injected Ctrl chords,
  ignores only the assistant's own `SendInput` events (stamped with a
  `dwExtraInfo` fingerprint) plus lower-integrity injection, and queues UI
  actions without calling Tk from its hook.
- The UI can launch `yolo-detection/live_view.py` as a second process for
  target-aware attacks and main-screen rope sensing.
- `FocusWorker` releases held keys on a focus dip, tries to refocus the game,
  resumes after a short transient dip, and stops patrol after sustained loss.
- Scheduled shutdown is temporarily hidden and its preserved `ShutdownWorker`
  is not started.
- `CountdownWorker` independently repeats an adjustable countdown, plays
  `sound/dingdong.mp3` at zero, and immediately resets for the next interval.
- The optional **掉线警报** consumes `CharacterWorker`'s existing yellow-marker
  result and raises an alert after three consecutive missing frames.
- The optional **测谎警报** samples the shared full-client capture every one
  second and alarms when it finds a HUD-scaled `#c9ced0` square.
- The independent **声音提醒**, **闪烁提醒**, and **消息提醒** choices deliver
  every countdown, disconnect, and lie-detector event through ding-dong audio, two
  red full-screen flashes, and Telegram respectively.
- The optional **消息提醒** queues those same events to a separate Telegram
  worker with a machine marker, event type, and local timestamp. The worker
  auto-detects the local HTTP proxy (system proxy first, then common ports:
  Clash 7890, Clash Verge 7891, mihomo 7897, V2Ray 10808/10809, ...), caches
  the working one, and retries once with a fresh proxy on transport failure.

See [ARCHITECTURE.md](ARCHITECTURE.md) for worker wiring, state machines,
cross-process coordination, configuration ownership, and the complete file
inventory.

The **攻击模式** panel offers **固定攻击** and **跳跃攻击** (plus YOLO when it is
restored). 固定攻击 keeps its behavior unchanged: the independent **随机跳跃**
row (Alt, own `base + random gap` timer, base minimum 1.0s) may be enabled and
its jump events are queued to the motion arbiter. 跳跃攻击 bundles the jump
into every beat - Alt, then the configured attack key 300ms later - so the
随机跳跃 row hides in that mode and no jump event enters the arbiter queue;
the 按键/interval/random-gap config stays visible and the bundle repeats on the
stored interval cadence. Both modes show the actual range as
`(base, base + random_gap)`. The scheduled-shutdown row is temporarily hidden
and no shutdown worker is started.

## Recording and patrol behavior

The shared route is stored in the `recording` section of `user_config.json`. In the UI, select
a layer and record any desired combination of:

- **Left-most**
- **Rope**
- **Right-most**

Each recorded point is an independent action. A layer can therefore be:

- empty: stand still and attack;
- left-only: hold that patrol point;
- left + right: patrol horizontally without climbing;
- rope-only: go directly to the rope;
- left + right + rope: patrol, then climb.

Layers are executed in numeric bottom-to-top order. The selected patrol range
is contiguous. A character that lands below it climbs back; one above it drops
back. Return-to-route input suppresses attacks until the route is reached.

Layers may be deleted down to **none** (`Ctrl+Delete` / 删除楼层). Patrol
stays startable with zero layers: with no route the character stands still on
the spot and only attacks/jumps there (stand-still mode, same as an empty
recording). Recording requires at least one layer; 添加楼层 after zero starts
over at 楼层1.

`patrol_cycles_per_layer` in `system_config.json` -> `rope_calibration` defaults to `2`. Every
layer completes two full horizontal Left/Right cycles before it climbs or,
for the top layer of the active route, drops back to the first route layer.
The final layer's rope is intentionally not used because no patrolled layer is
above it.

Adaptive recordings include `coordinate_v2` metadata based on minimap canvas
and diamond dimensions. Legacy ratio-only points still run, but should be
re-recorded when the UI labels them as a legacy layout.

When the saved and live minimap canvases are the same geometry (allowing one
pixel of capture rounding), patrol uses the recorded normalized point directly.
It does not re-project that point from a fluctuating yellow-diamond size. This
keeps a recorded rope X stable after a monster knockback while retaining
adaptive projection for a genuinely resized or moved minimap.

Recording does not require patrol to be active. After **重置录制**, the next
record-button click focuses the game, resets stale minimap geometry, and takes
up to three fresh one-off samples. A point is accepted only after an OpenCV
minimap border and yellow character diamond are both detected, which avoids
machine-specific failures caused by an overlapping UI or a transition frame.
Every successful recording also replaces the normalized `minimap_calibration`
in `user_config.json`. Patrol consumes that saved border independently, so its
startup does not depend on a recent recording frame or stable contour voting.

## Movement and safety rules

- Only the movement worker owns directional movement and its paired pickup
  hold. Z is pressed and released with Left/Right during walking.
- Rope approach uses minimap X for navigation. Fresh YOLO rope/character boxes
  refine the jump direction and timing but do not control the route.
- Rope-stall recovery is armed only after repeated no-progress frames within
  the rope alignment range. While the rope is farther away, movement keeps
  walking across the platform and cannot start a recovery jump. After a
  knockback, a far-away rope target that remains visually stationary for six
  frames releases and re-arms the walk hold; it does not turn that situation
  into a climb jump.
- Stair adaptation requires ten consecutive no-progress frames (about 2.5
  seconds). Progress is measured cumulatively from a stable anchor, so several
  small but real movements reset the counter and attack animations do not
  trigger false stair jumps.
- The minimap coordinate frame must stay stable for those no-progress
  counters to mean anything. `_stabilize_boxes` in `minimap_detector.py` holds
  one established box while the raw contour alternates between two
  near-identical boxes (border vs canvas edge - the live client flipped
  87x70/80x70 every few frames). Without the hysteresis anchor the normalized
  position swings +/-0.05 while the character stands still, which resets the
  stall counters and a stuck character presses Left forever without ever
  triggering edge recovery. A different box is adopted only after the SAME
  box repeats for a full history window (a genuine resize, HUD scale change,
  or map switch).
- When a walk key cannot be sent because the game window is not foreground or
  live input is disabled, `_send_walk_hold` logs at INFO
  (`walk key <dir> send blocked (window not foreground or input disabled)`)
  so a frozen character is diagnosable directly in the log instead of a
  silent no-op.
- Climb arrival requires the expected next layer and stable confirmation. Up
  remains held through a short rope-top compensation window.
- Rope jumping keeps two separate horizontal tolerances: only the narrow
  center zone jumps Alt+Up; positions outside it jump Left/Right toward the
  rope even when the wider attachment-verification band accepts their X.
- Attack suppression ends immediately after confirmed climb arrival; the
  separate arrival grace period only prevents an unsafe stair jump.
- Fixed attack timing is `base interval + random gap`. The shorter base slider
  sets the lower bound; adjacent −/+ buttons change the random-gap ceiling in
  0.1-second steps. The UI displays the complete effective range
  `(base, base + random gap)`, and both values persist in `user_config.json`.
- Losing focus releases every key. After refocus, movement reconciles its
  internal hold state with the sender and re-presses externally released keys.
- A secondary character-marker reading can override movement only when it
  belongs to the same frame and detected minimap region; clipped startup
  readings such as `marker_y=0` are ignored.
- Patrol startup detects the character's actual recorded layer from the fresh
  adaptive minimap before anchoring world Y; it does not assume the character
  is already on the configured first patrol layer. Startup refuses to arm
  movement when the marker is missing or does not match any recorded layer,
  instead of silently assigning the configured first layer.
- Layer detection fuses marker Y and scroll-compensated world Y instead of
  letting OpenCV tracking override the visible marker unconditionally. When
  marker Y matches exactly one recorded layer, that unambiguous layer wins;
  world Y resolves only overlapping/aliased marker bands and must itself fall
  inside a recorded world band. A confirmed rope arrival immediately
  re-anchors world Y to the new layer, so a stale lower-floor reading cannot
  turn a valid final-layer patrol back into the preceding layer before drop.
- Monster knock-downs are reconciled world-Y-authoritatively: the raw marker Y
  is screen-relative on a scrolling minimap and the OpenCV world-Y tracker
  lags a fast fall, so after a fall stops the landing floor is resolved from
  the raw world-Y reading once it stabilizes (3 frames / ~0.15 diamonds, or a
  1.2 s timeout), and the tracker is re-anchored to the TRUE landing layer -
  layer1, layer2 or any other.  A landing at/below the lowest recorded band
  always resolves to the bottom floor (nothing lower exists).  Without this a
  knock-down could leave the world-Y origin on the pre-fall floor, climb
  attach/arrival verification (world-Y progress based) would misread, and the
  character could keep walking the stale route instead of returning.
- The world-Y origin is kept honest continuously: while cruising on a
  confirmed floor a drift watchdog re-anchors the tracker when incremental
  phase-correlation drift exceeds its threshold (default 0.35 diamonds,
  checked every 2 s), and the idle "pin" only masks the world-Y reading while
  the marker Y still agrees with the believed floor - after a knock-down an
  off-band marker leaves the raw reading visible to reconciliation.
  Tuning lives in ``system_config.json`` -> ``rope_calibration``
  (``fall_settle_min_frames``, ``fall_settle_epsilon``,
  ``fall_settle_max_seconds``, ``world_drift_check_interval_seconds``,
  ``world_drift_reanchor_threshold``).
- A layer's marker-Y band is `(highest recorded Y - y_tolerance,
  lowest recorded Y + y_tolerance / 3)`. The full upper margin covers
  climb/drop arrival motion; the smaller lower margin absorbs OpenCV marker
  precision noise around the confirmed layer base without excessive overlap.
  The full recorded point span still covers stair-shaped layers.
- Stair-shaped layers retain the recorder's canonical anti-alias anchor, but
  also use the saved per-point `observed_world_y` values when their changes
  agree with adaptive diamond-space Y. This creates a real world-Y interval
  for paths such as left-high/middle/right-low. Confirmed landings interpolate
  the re-anchor from character X; airborne frames are never used as anchors.
- A bench or stair jump inside the current logical layer is treated as a
  same-layer bounce and never re-anchors world Y. When a rope climb actually
  begins, the live character X selects the recorded point-specific world Y;
  therefore a rope recorded while standing on a bench uses that bench anchor
  instead of the layer's flat fallback. Return/fall recovery re-anchors only
  after the landing layer is confirmed, then restarts patrol or returns from
  an out-of-range floor such as layer1.
- A marker-only position verifier runs every 0.75 seconds using the existing
  movement observation. Two matching out-of-range readings clear stale
  climb/drop state and start return-to-route without another capture or image
  analysis pass.
- Self-rescue requires a genuinely stationary run. Off-layer readings use a
  separate consecutive counter, so adaptive minimap drift or visible patrol
  progress cannot cause a premature Alt+Down drop.
- On scrolling minimaps where several floors share the same marker Y, final
  drop arrival uses the scroll-compensated world Y to distinguish the route's
  first floor. This prevents a layer3 drop from resetting immediately to
  layer2 before any drop input is sent.
- Final-layer descent cannot complete until at least one Alt+Down chord has
  actually been sent. If a lower layer's stair/bench band overlaps the final
  layer, the marker's nearest recorded base wins; world Y is used only for a
  genuinely tied/aliased marker coordinate. This prevents a stale lower-floor
  world anchor from resetting the route while the character remains upstairs.
- Missing or stale cross-process state files mean “not busy,” preventing a dead
  process from permanently blocking patrol or attack.

## Configuration

Configuration is intentionally split by ownership:

- `user_config.json` contains UI-managed values, route recordings, and the
  recording-owned minimap border: `minimap_calibration`, `recording`, `drug`,
  `fixed_attack`, `additional_functions`,
  `yolo_detection`, and `ui_window`. It is ignored and excluded from releases,
  so copying an update over an installation preserves every user choice.
- `system_config.json` contains update-owned internal behavior, currently
  `rope_calibration`. It is tracked and included in every release, so fixes such
  as `stair_jump_stall_frames: 10` replace obsolete system values during an
  update.

On the first split-config launch, `config_store.py` migrates user-owned sections
from the former unified `config.json` (or older split JSON files) into
`user_config.json`. It deliberately does not migrate `rope_calibration`; the
shipped `system_config.json` is authoritative. The old `config.json` remains as
a backup but is no longer written. The lower-level YOLO engine's developer
`yolo-detection/config.yaml` remains separate.

Runtime state and logs are written under ignored directories such as `work/`,
`outputs/`, and `recording-assets/`. Do not assume these files are saved by Git.

## Temporarily disabled YOLO monster detection

YOLO monster detection is temporarily disabled because the current
`weights/best.pt` is not trained reliably enough. The implementation and model
files have deliberately not been deleted. While disabled:

- the UI forces **Fixed Attack**, completely hides the YOLO panel and YOLO
  mode selector, and cannot launch the subprocess when patrol starts;
- fresh configuration defaults to `fixed` attack mode; and
- `install.ps1` skips the large PyTorch/Ultralytics dependency download. The
  preserved commands are inside the
  `YOLO_DEPENDENCIES_TEMPORARILY_DISABLED` block comment.

To recover YOLO quickly after replacing `yolo-detection/weights/best.pt` with a
better-trained model:

1. In `ui_worker.py`, change `_SHOW_YOLO_PANEL = False` to `True` so the
   preserved panel and YOLO attack-mode selector are packed into the UI again.
2. In `ui_worker.py`, change
   `_YOLO_MONSTER_DETECTION_ENABLED = False` to `True`.
3. In `install.ps1`, remove the opening
   `<# YOLO_DEPENDENCIES_TEMPORARILY_DISABLED` line and its matching closing
   `#>` line, then run `安装.bat` once to install the preserved dependencies.
4. Optionally change `config_store.py`'s default `fixed_attack.attack_mode`
   from `fixed` to `yolo`; otherwise select YOLO in the restored UI.
5. Update the temporary wording in `安装.bat`, `build_release.ps1`, this section,
   and `ARCHITECTURE.md`, then run the normal release workflow below.

The minimap-based patrol, layer, rope, fixed attack, potion, and alert workers
do not require these YOLO dependencies and continue to operate normally.

## Current work handoff (agents taking over)

Branch: `feature/continue-development`. Latest release: see `VERSION` and the
latest commit message. Push before handing off; keep user-owned JSON files
(`drug_settings.json`, `additional_functions_settings.json`,
`fixed_attack_settings.json`, `recording-configuration.json`,
`ui_window_settings.json`) UNCOMMITTED - they are runtime-modified on the live
machine.

### 2026-09-04 session (v0101 - v0107): knock-down recovery + world-Y anchoring

Bug chain: after a monster knock-down to below-range layer1 (map scrolls in Y,
so raw marker Y is screen-relative; OpenCV world Y is authoritative but lags
fast falls and drifts via incremental correlation), the character could end up
stuck with "weird" Alt+Down drops and failed return climbs.

- **v0101** Self-rescue is state-aware: in-range stuck restarts in place (no
  drop out of the range); below-range / return-mode never Alt+Downs, re-runs
  the return climb, and stops patrol cleanly after `rescue_cycle_limit`
  consecutive failures. `_drop_to_first_layer` treats a marker at/below the
  bottom floor band as "already arrived" (no more 30-chord drop storms).
- **v0102** Under-rope recovery: two plain Alt+Up attempts first, then sideways
  climb jumps alternating left/right (worker-level side + streak). Directional
  climb chords now hold Up from the START of the jump (persistent path) - the
  old chord-then-Up activated ~100ms late and missed rope grabs.
- **v0103** Rescue pre-flight probe `_rescue_verify_stuck_by_probe`: block
  attacks ~2s, force walk right then left; if the marker moves the rescue is
  cancelled (no drop/restart) - kills false rescues caused by attack-freeze.
- **v0104** TRUE layer recognition via world-Y re-anchoring:
  `_resolve_fall`/`_reconcile_landed_floor` wait for the tracker to settle
  after a fall, classify by world-Y bands over ALL layers (marker Y only as an
  unambiguous fallback) and re-anchor to the true layer; at/below the lowest
  band resolves to the bottom floor (also added to `_detect_floor_all`). The
  idle world-Y pin only masks while the marker Y agrees with the believed
  floor; raw world Y is captured per frame; a drift watchdog re-anchors while
  cruising. New tunables (fall_settle_*, world_drift_*, rescue_*, lateral)
  shipped in `system_config.json`.
- **v0105-v0107** UI: 调试日志 replaced by a 运行日志 LabelFrame panel that
  shows only significant events (patrol started/ended incl. external stops,
  ERROR+) as plain hint-style text; two top-right icon buttons appear when
  patrol has ended (archive = copy the in-memory 600-line run log, person =
  copy user settings); the log lives in memory only (UiLogHandler deque, no
  per-line disk I/O). Patrol-state sync is debounced over two polls so a
  transient self-rescue toggle never looks like a patrol stop.

### HUD geometry (measured on the real client, 1366x768 reference)

HUD regions are ABSOLUTE client pixels at the HUD reference size (client
width >= 1366px); below that `hud_scale_for(client_width)` scales every
region (0.75x at 1024x768). Key constants live in `assistant.py`:

- Minimap: `MINIMAP_REGION_TOP = 50`, `MINIMAP_FALLBACK_REGION = (0, 50, 400, 320)`.
  The map-name strip is ~64px tall ABOVE the minimap; the search region starts
  at y=50 (14px tolerance). Candidates below 25% of the region's min side are
  rejected so a ~40px UI box inside the map-name strip can never become the
  minimap border (`window=(5,22,45,62)` was the live failure).
- `MINIMAP_ANALYSIS_SIZE = (400, 400)`, aspect-preserving fit, never upscale.
- **v0043: `_stabilize_boxes` hysteresis anchor.** The live client flipped the
  minimap detection between 87x70 and 80x70 every few frames; the
  per-coordinate median flipped with the alternation, swinging the normalized
  position +/-0.05 while the character stood still, which defeated the
  stall/edge-recovery counters (stuck character pressed Left forever). The
  stabilizer now holds the first established box and adopts a different one
  only after the SAME box repeats for a full history window.
  `movement_worker._send_walk_hold` also logs at INFO when a walk key send is
  blocked (window not foreground / input disabled).
- **v0033: `analysis_box` now EQUALS `window_box`.** The yellow marker/patrol
  overlay rectangle must coincide with the green minimap rectangle. The legacy
  0.3125 top offset + 1.0513 right extension belonged to a layout with the map
  name INSIDE the minimap top; it made the yellow box shorter and wider than
  the green one. ROUTES RECORDED BEFORE v0033 were calibrated against the old
  offset box - re-record on the live machine after deploying v0033.
- When OpenCV cannot close a border contour, the marker-verified fixed region
  is promoted to `source="fixed-region"` (accepted by recording, startup
  probes, and calibration). `is_verified_border()` in `minimap_detector.py`
  returns True for opencv or fixed-region sources.
- Status bars: `STATUS_CAPTURE_WIDTH = 370`, `STATUS_CAPTURE_HEIGHT = 57`,
  bottom-centered (`STATUS_CAPTURE_CENTER_SHIFT = 0`). Inside the capture the
  three bars sit SIDE BY SIDE in one vertical band (rows ~33-53): HP red
  x ~7-91, MP blue x ~96-230, EXP yellow x ~237-363. `BarStatusDetector`
  (`status_worker.py`) measures each bar ONLY inside its own horizontal zone
  (`bar_zones`) with per-bar full widths (85/135/127px) - the three can never
  be mixed, and blue UI text above the band is excluded by `bar_band`.
  `StatusReading`/`work/status_state.json` carry `exp`/`exp_ratio` too.
- The live snapshot `work/debug/snapshot.png` (1707x1067) verifies: hp=38%,
  mp=84%, exp=69%; minimap window==analysis==(6,89,152,231) with the 6x6
  marker found inside.

### Pending / open items

- **Patrol turns left before reaching the recorded right-most point** (user
  report 2026-08-30): log shows `pos=(0.334, 0.717) target=0.231 action=left`
  while the recorded right-most is 0.4611 - the character reversed at x=0.334
  before reaching 0.4611. Suspects: `_force_advance_phase` (stair-jump give-up
  at line ~2406) firing early, or `_advance_route_endpoint` tolerance
  (`_current_horizontal_tolerance`) being too wide. NOT YET DIAGNOSED - check
  `movement_worker.py` `_stair_jump_decision` / `_force_advance_phase` call
  sites and the phase advance against the recorded `right_most_pos.x`.
- Re-record patrol points on the live machine after v0033 (analysis box
  change).

### UI layout (`ui_worker.py`)

- **Whole-window geometry, fixed-width columns.** The default window is
  1086 x 680 LOGICAL px (`_INITIAL_WINDOW_WIDTH`/`_INITIAL_WINDOW_HEIGHT`)
  and stays DPI-unaware, so Windows scales it like every other app: at 150%
  the physical size is ~1629 x 1020 and at 125% ~1358 x 850.  The 680
  logical height is the natural content height measured on the real
  machine; nothing is clipped and no extra blank space appears.
- **Column 0 fixed 550px, column 1 fixed 500px** (grid minsizes, both
  `weight=0`) + 12px gap + 24px container padding = 1086px.  Left column:
  patrol/attack/countdown/extra controls (警报 row fits on one line).
  Right column: drug/增益, quick messages, and the compact 运行日志 panel.
  `container`/`columns`/debug frame all use `pack_propagate(False)` so
  content can never spread the window at startup.
- **In-window caption row (34px, `_build_caption_bar`).**  The native
  caption strip is removed once via Win32 `WS_CAPTION` (resize borders,
  min/max flags, system menu and taskbar entry all stay), replaced by a
  Tk row: title `Maple 助手 (vNNNN)`, a `?` help button, and `－ □ ×`
  window buttons.  Dragging the caption row moves the window with an
  outline-only overlay (`_caption_drag_begin/move/end`) so drags never lag.
- **Resize is pure Tk** (`_install_resize_burst_guard`): a root-only
  Configure-burst detector pauses the heavy snapshot render while a
  resize burst is active and resumes ~300 ms after it settles.  No Win32
  window-proc subclassing and no `WM_SETREDRAW` (both crashed in earlier
  versions); startup renders immediately.
- **Window-height auto-fit for quick messages.**  Adding/editing/deleting a
  quick-message row calls `_refit_window_to_content()`, which sets the
  window height to exactly the taller column's required height + window
  chrome (clamped to minsize): rows added grow the window, rows deleted
  shrink it back, and no leftover padding is ever left at the bottom.
- **Persisted geometry is format-versioned** (`_GEOMETRY_FORMAT = 5` in
  `ui_window_settings.json`).  Records written by an older layout/caption
  semantic or a DPI-aware run (e.g. 1275x904, 1620x1050, 1635x1272) are
  ignored ONCE, so the window opens at the current default on every machine
  instead of restoring a stale manual size.
- Debug log panel: now 运行日志, a real LabelFrame panel (title + outline)
  like the other panels.  Inside, the messages are shown like the plain
  message hints of the 图层校准与巡逻 panel (normal ttk.Label text) and only
  significant events appear - patrol started, patrol ended, and ERROR+
  records (critical bugs) - as the latest few compact lines (older display
  lines scroll away; the full recent stream stays in memory).  The two icon
  buttons sit at the panel's top-right and appear once patrol has ended
  (report time): the archive icon copies the running log, the user icon
  copies the user settings JSON to the clipboard.  The latest 600 formatted
  log lines are retained in memory only (UiLogHandler history deque, oldest
  dropped like garbage); no per-line disk I/O is added by the UI log.
- The `?` help popup is hover-triggered (show ~180 ms after hover, hide
  after leave) and styled exactly like the yellow bind-key hint: single
  label, bg `#fffbd6`, fg `#202020`, relief solid, wraplength ~430.  Inside
  it the bindings render as an aligned two-column table
  (bold `按键 | 功能` header, keys right-aligned, functions left-aligned);
  the ten quick-message chords collapse to one line
  (`Ctrl+1 ~ Ctrl+0 → 发送第 1~10 条快捷消息`).  If the native caption
  cannot be removed, a small floating `?` at the top-right opens the same
  popup.

### Workflow

- Every behavior change always gets a new four-digit release package.
- Routine iterations do not repeatedly update these documents, run focused or
  full test suites, or create Git commits. Run tests only when the user
  explicitly asks (or when a change touches delicate timing/input logic such
  as movement, attack, jump, buff, or hotkey workers - then run only the
  affected module's suite). Do checkpoint tasks (docs, full gate, commit) only
  when the user explicitly says **update**, or immediately before context is
  nearly exhausted and the session must be compacted.
- ZIP verification through the former ignored `work\verify_zip.py` helper was
  redundant and has been removed from the publishing procedure.
- At a checkpoint, update `README.md` and `ARCHITECTURE.md`, run relevant tests,
  review/stage only intended tracked files, commit on
  `feature/continue-development`, and push when available.
- PowerShell uses `;` not `&&`; avoid `(Get-Content -Raw)` re-writes of UTF-8
  files (encoding corruption history: use the edit tool instead).

## Publishing a release

Every project update must produce a new package. **The standard routine
release is ONE command from the repository root:**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

It advances `VERSION` by one, rebuilds `release\MapleAssistant\`, and creates
`release\MapleAssistant-vNNNN.zip`; success prints
`release ready: <absolute zip path>`. `发布.bat` invokes the same script.
`-SkipTests` is the default for ordinary iterations - do not run tests for a
routine release unless the user asks.

For a requested update/checkpoint, run the script WITHOUT `-SkipTests`.
Note: the full gate is currently RED on one pre-existing, unrelated failure
(`test_ui_worker.UiLogHandlerTests.test_keysym_to_scan_key_limits_to_bindable_hotkeys`)
- repair or skip it deliberately when a checkpoint gate is requested.

The script performs two stages:

1. **Release-gate tests** using
   `yolo-detection\venv313\Scripts\python.exe` when present, otherwise
   `python`. Output is saved to `work\release_gate.log`; any failing test stops
   the release.
2. **Advance version, build, and ZIP.** `VERSION` is a four-digit counter from
   `0000` through `9999`. A missing file starts at `0000`; every later
   successful `release_now.ps1` run advances it once. The value is restored if
   the build fails. `build_release.ps1` receives that version. The destination
   `release\MapleAssistant\` is rebuilt from scratch, runtime files and model
   weights are copied, ignored development/test artifacts are excluded, old
   ZIPs are removed, and `MapleAssistant-v{0000..9999}.zip` is created. The
   packaged `VERSION` file drives the visible `Maple 助手 (vNNNN)` UI label.
Important publishing details:

- `build_release.ps1` must remain UTF-8 with BOM for Windows PowerShell 5.1.
  `release_now.ps1` repairs the BOM automatically when needed.
- Version `9999` is terminal: publishing stops with an error instead of
  wrapping back to `0000`.
- `release\` is Git-ignored. The ZIP is a delivery artifact, not part of the
  commit.
- `user_config.json` is user-owned and excluded from releases; never replace it
  during an update. `system_config.json` must be included and replaced so
  internal fixes follow the application version.
- A successful command prints `release ready: <absolute zip path>`.

At an update/checkpoint, save the source change after the release:

```powershell
git diff --check
git status --short
git add -- <changed source, tests, and documentation>
git commit -m "describe the completed change"
git status --short
```

Record the release ZIP name every time. Record the commit hash at checkpoints.

## Development checks

Run a focused test first, then the relevant module suite. For movement work:

```powershell
py -3.10 -m unittest test_movement_worker
```

The optional full checkpoint gate is defined in `release_now.ps1`; do not
maintain a second handwritten test list here.

Stop a console run with `Ctrl+C`. Keep the game visible during calibration and
use `--dry-run` whenever input injection is not intended.

The **Additional Functions** panel includes an independent **循环警报**. Set its
interval in hours, enable it, and use the remaining-time bar
to move the current deadline anywhere from zero to the full interval. For
example, with a `1.0h` interval, dragging the bar to `20m 00s` makes the next
event occur in 20 minutes. At zero, the selected reminder outputs run and the
bar resets to the full interval. The timer does not depend on patrol or attack
being active.  The 循环警报 interval and remaining-time sliders sit on one row
in the left column (the window is wide enough that they no longer wrap); the
remaining-time label is wide enough to show `1h 00m 00s` / `12h 00m 00s`
without truncation.  The 固定攻击/随机跳跃 base-time rows carry a
right-aligned random-gap group with an expanding slider.

Selecting **掉线警报** reuses the normal per-frame yellow-character-diamond
detection. Three consecutive missing frames confirm the loss and raise one
event; the alarm re-arms after the marker is detected again.

Selecting **测谎警报** checks one in-memory full-game frame every second.
Its signature is a block of the exact color `#c9ced0` (201, 206, 208) with a
±10 per-channel tolerance: 60x60 at a 1075px-wide client, scaled by the HUD
factor to 76x76 at 1366px and above (60 * 1366/1075; see
`lie_detector_worker.py`). A visible match raises one event; after the
square disappears, a later match can alert again. The detector never saves
screenshots, so no used image files remain to delete.

The reminder row controls event delivery independently: **声音提醒** plays
`sound/dingdong.mp3`; **闪烁提醒** makes the primary screen flash red for 0.5
seconds, turn off for 0.3 seconds, then flash red for another 0.5 seconds; and
**消息提醒** sends Telegram messages. Disabling sound does not disable either
of the other outputs.

Selecting **消息提醒** sends those same three alert events through
the configured BOT. Long-press the machine-name button for one second to turn
it into an input field; leaving that field saves it and restores the named
button. Enter a distinct **设备名称** on every computer, first send
any message (for example `/start`) to the BOT in Telegram, then click
**修改BOT token** and paste the token. The assistant verifies the BOT and learns
the latest chat automatically. The UI reports whether it is enabled, correctly
configured, awaiting a chat, or failed. Messages use
`设备名称 事件类型 时间 YYYY-MM-DD HH:MM:SS`. Token, learned chat ID, machine
name, and enabled state live only in ignored `user_config.json`, so updates do
not overwrite them. Telegram failures never stop other assistant workers.

The **快捷消息** panel lives beside the potion controls in column 2. Add any
number of reusable messages (maximum 20). Short-click a message to copy it to
the Windows clipboard; double-click it to focus the game, open chat with Enter,
paste with Ctrl+V, and send with Enter; long-press it for one second to edit it;
and long-press its adjacent `×` button for one second to delete it. Quick messages are
stored in the ignored `additional_functions_settings.json` (alongside the other
Additional Functions values) and survive application updates.  Adding,
editing, or deleting a row refits the window height exactly to the content
(grow on add, shrink back on delete) - no leftover blank space remains at the
bottom of the panel.

## Physical hotkeys and action sounds

`hotkey.json` owns the global bindings. `HotkeyWorker` uses a low-level Windows
keyboard hook. Every key event Maple Assistant injects itself is stamped with a
custom `dwExtraInfo` fingerprint (`SELF_INPUT_EXTRA_INFO` in `hotkey_worker.py`,
set by `WindowKeySender`); the hook ignores only those self-injected events
plus lower-integrity (`LLKHF_LOWER_IL_INJECTED`) injection, so the assistant's
own gameplay keys can never drive the chords while same-integrity
keyboard-sharing tools (Mouse Without Borders, remote-desktop clients) still
trigger them. `"ignore_injected": false` in `hotkey.json` accepts every
injected event. Unrelated keys pass through normally. The matched chord's
second key is consumed and each physical press fires once until released:

- `Ctrl+1` through `Ctrl+0` send the oldest ten quick messages. The binding is
  positional: deleting a message shifts every later message/key forward.
- `Ctrl+Left` records the selected layer's left-most point.
- `Ctrl+Up` records the selected layer's rope point.
- `Ctrl+Right` records the selected layer's right-most point.
- `Ctrl+Down` selects the next layer, wrapping from the highest layer to
  layer 1. The selected layer is visibly marked in the UI.
- `Ctrl+Home` selects the next patrol-start layer, wrapping from the highest
  layer to layer 1. The patrol end is never allowed below that start.
- `Ctrl+Insert` adds a new highest layer; `Ctrl+Delete` removes the highest
  layer (down to none - zero-layer patrol is startable stand-still mode).
  Adding a layer extends the patrol end to it; deleting clamps the
  patrol range to the remaining highest layer. These topology changes are
  deliberately blocked while patrol is active.
- Hold `Ctrl+[` or `Ctrl+]` to decrease or increase fixed attack base time in
  0.1-second steps. Releasing the chord for two seconds plays the single
  success confirmation sound. The fixed attack base time allows a minimum of
  0.2 seconds; the random jump base time minimum is 1.0 seconds (a jump
  motion window occupies 0.9s).
- `Ctrl+grave` (Ctrl plus the backtick key) toggles Start/Stop Patrol.

Recording is blocked while patrol is active. A successful recording or patrol
start plays `sound/success.mp3`; a recording failure, blocked recording, or
patrol stop plays `sound/fail.mp3`. Playback runs outside Tk so it cannot freeze
the UI. Ordinary internal failures do not play either sound. Except for the
intentional repeating `Ctrl+[`/`Ctrl+]` adjustment, each hotkey action has a
two-second cooldown and a held chord fires only once until its target key is
released.
