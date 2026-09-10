# Maple Assistant

Maple Assistant is a Windows helper for a MapleStory client. It captures the
game window, detects the minimap and player marker, follows recorded multi-layer
routes, handles ropes and recovery, performs configured attacks, watches HP/MP,
and provides alerts and quick messages.

> Automation may violate a game or server's rules. Use it only where permitted.

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
recording uses a short focused capture of its own.

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

### HP/MP and timed rows

HP and MP potions are urgent direct actions. They use bar ratios rather than
OCR numbers, retry blocked sends, and verify that a bar responds.

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
during patrol. The hook ignores assistant-generated input and does not steal
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

## Auto lie pass (自动过测谎, optional fade-target tracker)

A bottom row of the **附加功能** panel hosts the automation checkbox and one
package-matched replay tester:

- **自动过测谎** checkbox: when armed, the assistant does not only *alert* on
  a lie square - it also drives the real mouse onto the fading target.  The
  #c9ced0 slice is only the **alarm** (the assistant plays `dingdong.mp3`);
  four seconds later Cutie takes over inside the lie popup, follows the
  target while it dims and moves the cursor onto it.  **No click is ever
  sent** - the server passes on cursor presence - and the game window
  regains focus by itself.  A lost track is never recovered (the cursor is
  left in place).  Lie events are rare and single-shot by design.
- CPU releases show only **测试 CPU**; CUDA releases show only **测试 CUDA**.
  A development checkout without a release manifest may show both comparison
  buttons. The published button reuses the already-preloaded package model,
  so it does not wait for another Cutie load.

How it works:

1. The bundled `target_tracker/` component (Realtime Fade Tracker: Cutie VOS
   + dense optical-flow residual path) is imported from source
   (`target_tracker/src`). `release_variant.json` locks a published package to
   its CPU or CUDA device; only a development checkout uses automatic choice.
2. `LieDetectorWorker` forwards each newly detected #c9ced0 square through
   an optional `lie_seen_callback` (the diagnostic recorder subscribes the
   same way).  The slice is the event *alarm*: it disappears as soon as the
   real popup opens, so its absence never stops the tracking.
3. `AutoLieWorker` preloads the package's Cutie weights as soon as the UI
   opens. The status line reports preloading, ready, or failure.  Four
   seconds after the alarm it crops live frames to the lie popup only - the
   tracker never sees the rest of the game client - finds the bright-white
   target inside it (the `_MIN_LIE_TARGET_SIZE` guard rejects the popup's own
   white countdown digits) and seeds Cutie.  It then follows the target while
   it dims, maps popup pixels onto the popup's screen rectangle and moves the
   mouse; **no click is sent**.  A sequence ends when the checkbox is turned
   off, the tracker loses the target, or the 45 s safety cap is reached.
   Cutie models are loaded once per process and reused, because Cutie's Hydra
   init cannot run twice in one process.  Popup geometry is measured on the
   1366x768 preset: 1920x1080 uses the **same preset**, so the popup pixels,
   the countdown guard and the detector's accepted popup SIZE in pixels are
   identical there (`_lie_window_size_bounds()`); the whole set is scaled by
   the fixed width ratio only on a smaller client such as 1075x768.
4. Enabling 自动过测谎 automatically enables the 测谎 detection (its event
   source). Turning 测谎 off also turns 自动过测谎 off.
5. `lie_screenshot_recorder.py` records the whole client for
   `RECORD_SECONDS` after any alert (lie / 掉线 / 循环), then turns the run
   into a single mp4 with `lie_video_tools.compose_frames_to_video` and
   deletes the frames unless `keep_frames=True`.  The same module can replay
   such a recording through the real engine in-game-style
   (`simulate_in_game_run`: annotated video + per-frame text log, mouse
   replaced by a recorder), so a lie event can be validated without the game.

Availability probing happens at UI build (`probe_auto_lie_environment`):
missing torch/cutie/weights or a missing `target_tracker` folder disables
nothing - the status text under the row explains the exact reason, and the
测试测谎 button always reports missing components in a popup instead of
failing silently.  Related log lines start with `auto-lie:` / `lie-detect:`
so the whole event chain is traceable in `error.log`.

Installation behavior (see `install.ps1`):

- pip mirrors fall back TUNA - Aliyun - Tencent - official PyPI; the venv is
  self-healed when pip is missing (ensurepip, recreate, get-pip.py).
- The VC++ 2015-2022 runtime is auto-detected (6 DLLs) and installed from
  the bundled `target_tracker/offline_bundle/installers/vc_redist.x64.exe`
  (offline) with an aka.ms fallback.
- Windows 10 (build < 22000) uses the compatible legacy CPU Torch/NumPy pair;
  NumPy is corrected before Torch first imports. CPU installs resolve a
  compatible Torch/Torchvision pair and verify both Cutie installation and
  `import cutie`. A published CPU/CUDA installer fails clearly if its required
  tracker environment cannot be completed.
- The initial UAC helper closes at once. The elevated installer window remains
  open only to show progress and the final success/failure result.
- On startup the assistant clears every previous `*.log` (single-instance
  check happens first), and all levels (INFO+) are written to the single
  `error.log` file; `work/assistant.log` no longer exists.

Diagnostics:

- The **运行日志** panel gained an alert-triangle (❗) icon button that
  copies the whole root `error.log` to the clipboard.
- `diagnose.bat` prints OS build, CPU, VC DLL presence, registry version,
  Python and the full torch import error - the standard first response for
  WinError 1114 reports.

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

## Important files

| File | Responsibility |
|---|---|
| `assistant.py` | application wiring and lifecycle |
| `auto_lie_worker.py` | optional GPU/CPU fade-square tracker for 自动过测谎 (AutoLieWorker + environment probe) |
| `lie_demo_player.py` | 测试测谎 demo window replaying `detect_video/try_detect.mp4` |
| `lie_screenshot_recorder.py` | alert-triggered full-client diagnostic recorder; composes its run into one mp4 |
| `lie_video_tools.py` | frames -> mp4 + in-game replay test (H.264 via ffmpeg when available; decoupled, never moves the mouse) |
| `ui_worker.py` | Tk UI, recording, settings, update/export actions |
| `movement_worker.py` | patrol, rope, fall/recovery, directional ownership |
| `motion_arbiter.py` | serialized random-jump/buff/small-step actions |
| `stair_jump_worker.py` | one-at-a-time stair Alt tap that preserves patrol walking |
| `status_worker.py` | HP/MP bars, potions, timed drug/buff scheduling, shared key sender |
| `attack_worker.py`, `random_jump_worker.py` | attack and optional jump timing |
| `quick_pickup_worker.py` | Ctrl+Z manual-only rapid pickup when patrol is stopped |
| `timer_state.py` | atomic persistence for the independent circular-alert deadline |
| `trade_worker.py` | Ctrl+Q/Ctrl+W virtual-input trade flows and invisible presence check |
| `patrol_control.py` | route model and persistence |
| `minimap_detector.py`, `marker_detector.py`, `map_structure_tracker.py` | minimap geometry, marker detection, world-Y tracking |
| `config_store.py`, `update_manager.py` | configuration ownership, import/export, and self-update |
| `error.log` | rotating fatal-error log, generated beside the application |
| `diagnose.bat` | one-shot environment diagnostic (OS/CPU/VC DLLs/torch import) for WinError 1114 reports |
| `target_tracker/` | bundled Realtime Fade Tracker component (own docs inside; src imported via sys.path) |
| `detect_video/` | demo footage for 测试测谎 (`try_detect.mp4`) |
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
