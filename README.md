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
(highest recorded Y - y_tolerance,
 lowest recorded Y + y_tolerance / 3)
```

The smaller lower allowance absorbs recognition noise without merging nearby
layers. Recorded stair/bench points may have their own world-Y anchors. A
normal bench/stair jump stays on the same logical layer; only a confirmed climb
arrival or settled fall re-anchors world Y.

Movement owns directional keys and paired Z pickup. It releases and re-arms
these holds around focus dips, recovery, and route changes to prevent stale
directions freezing the character. Rope navigation is minimap-based; YOLO is
not required.

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
their 声音 / 闪烁 / 消息 outputs, 自动重连 (armed by 掉线, confirmed by the login
page's base colour, with a temporary **测试自动重连** button and the 5-point
**标定选择窗口** wizard), and the single **测试api** button that runs a video
through the remote RoiTrack service (see below).

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

## Optional alerts

Alert sources are **掉线**, **测谎**, and **循环**. Output choices are independent:
**声音** plays `sound/dingdong.mp3`, **闪烁** flashes red twice, and **消息**
sends Telegram with machine name, event type, and time.

- 掉线 reuses the yellow marker result and alerts after three missing frames.
- 测谎 scans the shared capture once per second for a resolution-scaled white
  square and saves no screenshots.
- 循环 is an independent draggable countdown; it does not depend on patrol.
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
test suites merely as a routine step. Documentation-only edits do not require
tests or a release ZIP. `release_now.ps1` advances `VERSION`, rebuilds both
`release/MapleAssistant-CPU` and `release/MapleAssistant-CUDA`, then produces
`MapleAssistant-CPU-vNNNN.zip` and `MapleAssistant-CUDA-vNNNN.zip`. Once both
are successful, prior CPU/CUDA release ZIPs are removed. Version `9999` never
wraps.

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
| `ui_worker.py` | Tk UI, recording, settings, update/export actions |
| `movement_worker.py` | patrol, rope, fall/recovery, directional ownership, endpoint arrival bounds, stall watchdog |
| `motion_arbiter.py` | serialized random-jump/buff/small-step actions |
| `stair_jump_worker.py` | one-at-a-time stair Alt tap that preserves patrol walking |
| `status_worker.py` | HP/MP bars, potions, timed drug/buff scheduling, shared key sender |
| `attack_worker.py`, `random_jump_worker.py` | attack and optional jump timing |
| `quick_pickup_worker.py` | Ctrl+Z manual-only rapid pickup when patrol is stopped |
| `reconnect_worker.py` | 自动重连: login-page check, character select, channel entry, 5-point calibration |
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

- `VERSION` is **0390** and both `release/MapleAssistant-CPU-v0390.zip` and
  `release/MapleAssistant-CUDA-v0390.zip` exist (62.2 MB each); the previous
  pair is removed automatically. Next build would be 0391.
- 0390 carried: **自动过测谎 removed** (local Cutie/torch pass, its replay demo,
  overlay, engine, offline weights, installer step) and **测试api rewired to a
  video-only drill** against the remote RoiTrack service - one button, no 时长
  box, no 密钥 button (the key ships in `autolie_api/key_secret.txt`), connect at
  once -> wait for the second window (3 s) -> feed ~27 s -> close when the clip
  ends, with mid-run server resets survived by reconnecting and restarting
  `frame_id`. The measured lie-popup geometry moved to `lie_geometry.py`.
- Earlier in the unreleased line: the RoiTrack client itself (`autolie_api/`:
  endpoints, WS transport, key store, run logs, local mimic), 自动重连 (login
  colour gate, world/channel entry, 5-point calibration, temporary 测试按钮),
  the JPG-only image policy (`image_io.py`), and the far "no progress" endpoint
  bound in `MovementWorker`.
- Open investigation: an in-game character freeze that only an assistant
  restart recovered. The next occurrence needs the `movement worker silent
  for ...` header plus the stalled movement worker's own stack; everything
  needed to capture those is already shipped.
- Open item: 自动重连 clicks land at client (0,0) until the operator runs the
  5-point **标定选择窗口** wizard once (the layout is deliberately uncalibrated
  until then, and the worker refuses to click).
- 9 pre-existing test failures are known and unrelated to these changes (757
  tests total): `test_config_store` 1, `test_movement_worker` 6
  (`centered_marker`, `descending_flag`, `off_route_marker`,
  `return_climb_finishes`, `return_climb_targets`, `return_to_first_layer`),
  `test_ui_worker` 2. `release_now.ps1 -SkipTests` is therefore the normal
  release command.
- Quota note: the real service bills one round per drill (`quota_left` is in
  every `handshake_ack` and frame answer). Offline checks use the mimic
  (`use_mimic=True` / `work/video_drill_check.py`) and cost nothing - verify
  there before spending a round.
- Pending feature: the **automatic** API lie pass (detect the lie event, then
  hand the popup ROI to the service and react) - the video drill is its
  prototype: same transport, same geometry, same wait/feeding order.
- Data note: a layer's saved 最左 may still carry a bad `recorded_layout`
  diamond (for example 15x7), which the diamond re-projection compensates for;
  re-recording that endpoint is the permanent cure.
