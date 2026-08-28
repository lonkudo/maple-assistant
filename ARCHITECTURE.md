# Maple Assistant Architecture

This document is the technical handoff for future development sessions. It
describes the current runtime, control ownership, patrol state machines,
publishing workflow, and repository inventory. Update it whenever files,
workers, state ownership, or release steps change.

## 1. System boundary

The repository also contains `boss_tracker/`, a separate Tk application with
its own process, persistent `boss_tracker/config.json`, tests, launcher, and
release workflow. It does not import Maple Assistant workers or participate in
game capture/input. Its channels use independent wall-clock deadlines under a
single universal interval measured in minutes; legacy hour-based settings are
migrated without changing the real countdown duration, while statistics are
persisted atomically.

The project has one primary process and one optional subprocess:

```text
assistant.py (primary process)
  main thread: Tk UI, or waits when --no-ui
  worker threads:
    CaptureWorker      capture + latest-frame fan-out
    CharacterWorker    independent minimap character position stream
    MovementWorker     route, walk/Z, stairs, climb, drop, recovery
    StatusWorker       HP/MP detection, potions, buffs
    AttackWorker       optional fixed-rate attack
    FocusWorker        foreground gate, refocus, key release
    ShutdownWorker     optional timed shutdown
    CountdownWorker    independent repeating MP3 reminder
    LieDetectorWorker  1-second full-client pure-white-square alarm
    ScreenBlinker       optional two-flash blue visual alarm
    supervisor-worker  stops the process if a core worker dies

yolo-detection/live_view.py (optional subprocess launched by UiWorker)
  screen capture -> YOLO inference -> target decision -> AttackExecutor
  also publishes character/rope screen geometry for climb gating
```

`assistant.py` acquires a Windows single-instance mutex, constructs all shared
events and latest-only queues, wires workers, starts the core threads, and owns
shutdown. Imports are delayed so `assistant.py --help` works before optional
dependencies are installed.

## 2. Frame and position flow

`CaptureWorker` captures the full client rather than a fixed top-left crop.
Normalized regions are mapped into the current client size. A `FrameBus`
publishes the newest frame to bounded size-one queues for movement, status,
character, and (when enabled) UI consumers. Slow consumers discard old frames
instead of building latency.

The analysis path is:

```text
game client
  -> CaptureWorker
  -> FrameBus
     -> movement_frames  -> MovementWorker
     -> status_frames    -> StatusWorker
     -> character_frames -> CharacterWorker -> character_positions
     -> lie frames       -> LieDetectorWorker (one in-memory scan per second)
     -> ui_frames        -> UiWorker
```

`MinimapDetector` uses OpenCV to locate the resizable minimap, inner canvas,
map-name crop, and analysis box. `marker_detector.py` locates yellow player and
red other-player diamonds. `DiamondSizeTracker` stabilizes small marker-size
changes but accepts large zoom changes immediately. `MapStructureTracker`
estimates scroll-compensated world Y and can re-anchor at a recorded floor.

`CharacterWorker` supplies a separately sampled marker position. Movement uses
it only when confidence is sufficient and its frame sequence and minimap region
exactly match the movement analysis. This prevents the startup fallback crop
from overwriting a valid layer marker with a clipped `marker_y=0` reading.
The optional disconnect alarm consumes that same detector result rather than
capturing or detecting again. Three consecutive missing results trigger one
background `sound/beep.mp3`; a later valid marker re-arms the next alert.

## 3. Input ownership and foreground safety

`WindowKeySender` in `status_worker.py` is the shared Win32 `SendInput`
implementation. It:

- finds one matching game window and stores its HWND;
- tracks per-key owners and physical down/up state;
- refuses live input while disarmed or unsafe;
- serializes sensitive input sequences;
- releases all owned keys during stop or focus loss.

The UI starts with input disarmed. **Start Patrol** prepares the map session,
selects the game window, and arms input. **Stop Patrol** disables input and
releases keys.

`FocusWorker` checks whether the selected game is foreground. During a focus
dip it clears automation events and releases all keys. It attempts a rate-
limited asynchronous refocus. A short dip resumes automatically; a sustained
loss disables input and marks patrol stopped while leaving the UI open.

Because focus release occurs outside `MovementWorker`, the movement worker
reconciles its private Left/Right/Z hold bookkeeping with
`WindowKeySender.is_key_down()` before extending a hold. This re-arms keys that
were externally released and prevents a repeated movement decision with no
physical input.

Runtime log formatting removes the redundant `-worker` suffix from thread
names. Movement stage logs are compact (`PATROL|`, `CLIMB|`, and
`MOVE TO ROPE|`) without alignment padding before the separator.

Control ownership rules:

- `MovementWorker` owns Left, Right, Alt+Up, Up, Alt+Down, and walking Z.
- `StatusWorker` owns potion and buff keys.
- `AttackWorker` owns the configured fixed attack key.
- `AttackExecutor` in the YOLO subprocess owns facing and the configured YOLO
  attack key, subject to patrol-state gating.
- `pickup_worker.py` remains in the repository, but the primary runtime no
  longer starts it; pickup is integrated into movement so Z and direction are
  pressed and released together.

## 4. Patrol data model

`PatrolController` is the thread-safe owner of the `recording` section in
`config.json`. It provides immutable snapshots to the UI and
movement worker and persists updates atomically through a temporary file.

Each layer may contain any subset of:

```text
left_most_pos -> right_most_pos -> rope_pos
```

Only recorded actions are included. Layers and active patrol bounds are sorted
by their numeric suffix, not recording order. The active range is the
contiguous slice from `patrol_start_layer` through `patrol_end_layer`.

Recorded points contain normalized X/Y. New recordings also store
`coordinate_v2` diamond-relative coordinates and recorded canvas geometry.
`PatrolController.snapshot(layout)` projects these stable coordinates into the
current minimap layout and scales layer tolerance.

Map identity is handled separately by `MapIdentityStore`, using ignored
reference images below `recording-assets/map-names/`. Starting patrol verifies
the configured map when a reference exists. The same fresh frame detects the
actual current layer in the adaptive minimap layout before world Y is anchored;
startup never assumes the character already stands on the configured first
route layer. Map structure reference data under `recording-assets/` supports
world-Y tracking.

## 5. Movement state machines

### 5.1 Layer route

For each active layer, `_layer_phases()` returns its available actions in
Left/Right/Rope order. Horizontal actions repeat
`patrol_cycles_per_layer` times (default 2) before rope or final drop.

```text
left -> right -> left -> right -> rope/climb
                               \-> final-layer drop
```

The exact sequence adapts to partial recordings. The final active layer omits
rope. With multiple active layers and `drop_to_first_layer` (or an explicitly
configured range), it drops only after the final layer has completed all
horizontal cycles.

### 5.2 Layer detection

Screen-space floor detection uses a band from the minimum to maximum Y of all
recorded points on a layer, with tolerance applied above the topmost point.
World-space detection uses the corresponding scroll-compensated world-Y band.

`_resync_route_layer()` runs on fresh frames. During a climb it accepts only
the immediate expected next layer and requires stable frame/time confirmation.
A short Up compensation window clears the rope lip before route ownership is
released. During intentional descent or return-to-route, those dedicated
states prevent generic resync from hijacking the route.

### 5.3 Walking and endpoints

Far movement uses a bounded continuous hold; final correction duration is
computed from minimap X distance. Passing an endpoint line counts as reaching
it, avoiding an exact-pixel problem.

Left/Right walking holds Z simultaneously for pickup. Hold management is
non-blocking so frame analysis and coordination continue during long movement.
Stair-jump recovery watches progress toward a horizontal endpoint. After a
configured stationary run it holds the travel direction and taps Alt, with
grace and attempt limits. If that budget is exhausted, the route reverses, but
the new direction must first show real minimap movement away from the blocked
position; a stale marker cannot skip the reversal and retry the same endpoint.

### 5.4 Rope approach and climb

Outside the rope zone, movement walks or creeps toward the recorded minimap X.
Inside the zone, fresh YOLO rope/character boxes can select straight, left, or
right jump-climb timing. Stale/missing YOLO state falls back to minimap logic.
The platform-edge stall path requires both a rope-approach route and repeated
no-progress samples within the rope alignment threshold. Recovery also checks
that threshold itself, so a distant rope target always remains an ordinary
walk and cannot enter the jump-climb state machine.

The climb state machine performs the jump chord, holds Up persistently, tracks
screen/world-Y progress, retries failed grabs, releases a stalled climb, and
confirms arrival at the expected next floor. Walking decisions are deferred
while persistent Up owns the climb.

Attack is suppressed during climb/return input and released immediately when
arrival is confirmed and Up is released. A separate climb-arrival timestamp
only suppresses unsafe stair jumps during settling.

### 5.5 Fall, return, descent, and rescue

- Unexpected rapid downward marker movement starts fall recovery.
- Landing inside the patrol range restarts or continues that floor.
- Landing below the range starts climb-to-route using each current floor's
  rope; landing above starts drop-to-route.
- Final-layer descent owns the route until the first active layer is reached.
- Return-to-route blocks attacks for the entire recovery.

Self-rescue is a last resort for a missing marker or a stationary character.
The ordinary stationary detector measures displacement from a fixed anchor, so
many small movements accumulate into real progress. Off-layer readings have a
separate consecutive stationary counter; they cannot inherit ordinary stuck
frames, and visible X/Y progress prevents emergency Alt+Down. This protects a
valid upper-layer patrol from transient adaptive layer-band misses.

## 6. Attack and status modes

The fixed attack thread always exists but begins disabled unless requested or
enabled through the UI. It periodically presses the selected attack key and
honors climb/return suppression.

The UI can launch `yolo-detection/live_view.py`. That loop captures with MSS,
runs the model in `weights/best.pt`, filters detections to the configured zone
and mob size, chooses an in-range target, optionally invokes
`AttackExecutor`, publishes attack state, and publishes the nearest useful
rope/character geometry. The UI prefers a local
`yolo-detection/venv313`, then falls back to the assistant's current Python
environment.

`StatusWorker` analyzes the status capture independently. Potion use requires
confirmed low readings and retries blocked sends. Its configuration also owns
optional periodic buffs.

`CountdownWorker` has no gameplay dependencies: it owns only a monotonic
deadline, configured interval, wake event, and `sound/beep.mp3`. At expiry it
re-arms the full interval before playing the MP3 through the Windows MCI API.
The UI reads a locked snapshot for its live progress scale. Dragging that scale
sets a new remaining duration within `0..interval`; UI refresh pauses while the
pointer owns the scale so it does not fight the drag.

`LieDetectorWorker` receives its own latest-only subscription to the existing
full-client `FrameBus`. It does not capture again and never writes a screenshot.
Every second it scales the reference `40×40` signature from a
`1075×768` client to the current width and height, then uses an OpenCV erosion
over the exact-white pixel mask to find an all-white rectangle. A match beeps
once until a later scan confirms the square has disappeared.

`ScreenBlinker` is a separate, request-driven worker. When **闪烁提醒** is
selected, every existing beep trigger queues a 0.5s blue full-screen flash,
a 0.3s gap, and a final 0.5s blue flash
(countdown, disconnect alarm, and lie detector). It owns no capture or game
input and therefore cannot delay the workers that produced the alert. It uses
a native no-activation, topmost Win32 overlay to cover the game's virtual
desktop rather than a background Tk window, and the UI exposes a test button.

## 7. Cross-process coordination

The primary and YOLO processes coordinate through atomically replaced,
timestamped JSON files under `work/`. This is an advisory handshake, not a
cross-process mutex.

| State file | Writer | Reader | Meaning |
|---|---|---|---|
| `work/attack_state.json` | YOLO process | MovementWorker | A current mob target requests attack priority |
| `work/patrol_state.json` | MovementWorker | AttackExecutor | Climb/drop/return is busy; attack must wait; also carries facing |
| `work/rope_state.json` | YOLO process | MovementWorker | Fresh rope and character screen coordinates/boxes |
| `work/status_state.json` | StatusWorker | diagnostics/UI consumers | Latest HP/MP state |

Every reader treats missing, malformed, inactive, or stale state as not busy.
This makes startup order independent and prevents a crashed process from
wedging the other process.

Thread-local synchronization uses `threading.Event`, latest-only queues, the
sender's ownership lock, and a climb/attack critical section. Do not introduce
a blocking cross-process mutex without explicit abandoned-owner and crash
recovery behavior.

## 8. Configuration ownership

`config_store.py` atomically owns the single user file `config.json`.

| Section | Main owner/consumer |
|---|---|
| `recording` | PatrolController, MovementWorker, UiWorker |
| `rope_calibration` | assistant.py -> MovementWorker tuning |
| `drug` | UiWorker, StatusWorker, MovementWorker |
| `fixed_attack` | UiWorker and AttackWorker |
| `additional_functions` | UiWorker and optional-function workers |
| `yolo_detection` | UiWorker and YOLO launch settings |
| `ui_window` | Tk window geometry helpers |

Missing sections migrate once from the former split JSON files. `config.json`
is ignored and excluded from releases, so updates cannot replace user settings
or route recordings. `yolo-detection/config.yaml` remains the model
subproject's lower-level developer configuration.

Persistent user recordings and generated assets may be modified at runtime.
Avoid overwriting them during unrelated code changes.

## 9. Publishing workflow

The canonical release command is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1
```

`发布.bat` is the double-click wrapper. The workflow is:

1. Run the exact release-gate suite defined in `release_now.ps1`; save output
   to `work/release_gate.log`; abort on nonzero exit.
2. Ensure `build_release.ps1` has its required UTF-8 BOM.
3. Run `build_release.ps1 -Zip`, which recreates
   `release/MapleAssistant`, copies runtime files/assets/model weights, removes
   old ZIPs, and creates `release/MapleAssistant-{dd-HH-mm}.zip`.
4. Run the local verifier `work/verify_zip.py` against the newest ZIP; abort
   if verification fails.
5. Inspect `git diff --check` and `git status`, stage only intended source,
   tests, and docs, commit, and verify a clean worktree.

Every project update requires a new successful release and a new Git commit.
Release ZIPs are ignored and are not committed. `work/verify_zip.py` is also
ignored but currently required by the release script, so a fresh environment
must restore/provide it before publishing. Do not use `-SkipTests` for a real
release.

## 10. Repository file inventory

This inventory reflects the current tracked files. Regenerate the source list
with:

```powershell
git -c core.quotepath=false ls-files
```

### Root runtime and control files

| File | Responsibility |
|---|---|
| `assistant.py` | Primary entry point, dependency wiring, lifecycle, single-instance guard |
| `config_store.py` | Atomic unified settings store and split-file migration |
| `capture_worker.py` | Client capture, frame bus, region mapping |
| `movement_worker.py` | Patrol and movement state machines |
| `character_worker.py` | Minimap character-position stream and shared-result disconnect alert |
| `status_worker.py` | SendInput sender, status detection, potions/buffs |
| `attack_worker.py` | Fixed-rate attack thread |
| `focus_worker.py` | Foreground/refocus gate and key release |
| `shutdown_worker.py` | Optional timed shutdown |
| `countdown_worker.py` | Independent repeating countdown and MP3 playback |
| `lie_detector_worker.py` | Resolution-scaled in-memory lie-event detection |
| `screen_blinker.py` | Optional queued two-flash blue full-screen notifier |
| `pickup_worker.py` | Legacy standalone pickup worker; not wired by assistant.py |
| `channel_switch.py` | Channel-switch/drop recovery procedure |
| `combat_coordination.py` | Attack, patrol, and rope state-file adapters |
| `patrol_control.py` | Thread-safe route recording and persistence |
| `minimap_detector.py` | OpenCV minimap/canvas/name-region detection |
| `marker_detector.py` | Yellow/red diamond detection and size stabilization |
| `map_structure_tracker.py` | Scroll/world-Y tracking and re-anchoring |
| `map_identity.py` | Map-name visual reference storage/matching |
| `ui_worker.py` | Tk dashboard, recording controls, settings, YOLO subprocess |

### Root configuration, installation, release, and documentation

| File | Responsibility |
|---|---|
| `config.json` | Ignored user-owned unified settings, generated/migrated at runtime |
| Former split settings JSONs | Legacy migration sources only; excluded from releases |
| `sound/beep.mp3` | Countdown reminder sound shipped in releases |
| `yolo_detection_settings.json` | Saved YOLO UI settings |
| `requirements.txt` | Primary Python dependencies |
| `install.ps1`, `安装.bat` | Bootstrap Python, `.venv`, dependencies, launchers |
| `start_assistant.bat`, `启动助手.bat` | Elevated/normal application launch wrappers |
| `launch_assistant_elevated.vbs` | Development elevation helper; excluded from release |
| `restart_assistant.ps1` | Development restart helper; excluded from release |
| `build_release.ps1` | Minimal distributable folder and ZIP builder |
| `release_now.ps1`, `发布.bat` | Canonical gated release workflow |
| `README.md` | User/developer overview and operating workflow |
| `ARCHITECTURE.md` | Technical handoff and full inventory |
| `INSTALL.md` | Installation guide |
| `COMMIT_MSG.txt` | Historical/local commit-message material; excluded from release |
| `.gitignore` | Generated/runtime exclusion rules |

### Separate `boss_tracker/` application

| File | Responsibility |
|---|---|
| `boss_tracker/app.py` | Standalone Tk UI, channel progress bars, and alarm polling |
| `boss_tracker/model.py` | Atomic configuration, independent deadlines, and statistics |
| `boss_tracker/audio.py` | Non-blocking MP3 alarm and direct SAPI female Chinese announcement through bundled comtypes |
| `boss_tracker/test_model.py`, `test_audio.py` | Countdown, persistence, statistics, and speech-text tests |
| `boss_tracker/启动BOSS追踪.bat` | Standalone launcher |
| `boss_tracker/build_release.ps1` | Minimal separate package builder |
| `boss_tracker/release_now.ps1` | Tests, builds, and verifies the separate ZIP |
| `boss_tracker/requirements.txt` | Small pure-Python comtypes dependency bundled into the release |
| `boss_tracker/install_boss_tracker.ps1`, `安装.bat` | Beginner one-click Python 3.10, virtualenv, and Tsinghua-mirror setup |
| `boss_tracker/启动BOSS追踪.bat`, `launch_boss_tracker.vbs` | Starts the local virtual environment via hidden pythonw window |
| `boss_tracker/README.md` | BOSS Tracker operating and release guide |

### Root tests

```text
test_assistant.py
test_attack_worker.py
test_capture_worker.py
test_channel_switch.py
test_combat_coordination.py
test_character_worker.py
test_config_store.py
test_countdown_worker.py
test_lie_detector_worker.py
test_screen_blinker.py
test_focus_worker.py
test_map_identity.py
test_map_structure_tracker.py
test_minimap_detector.py
test_movement_worker.py
test_patrol_control.py
test_pickup_worker.py
test_shutdown_worker.py
test_single_instance.py
test_status_worker.py
test_ui_worker.py
test_yolo_decision.py
```

### `yolo-detection/` tracked files

| File/group | Responsibility |
|---|---|
| `live_view.py` | Live capture/detection/attack subprocess |
| `attack_executor.py` | Facing and attack input with patrol-state gate |
| `auto.py` | Model wrapper, configuration, detections, decision logic |
| `cn_text.py` | Chinese preview text rendering |
| `config.yaml` | Detector configuration |
| `requirements.txt` | YOLO/tooling dependencies |
| `start.py`, `start_py313.py` | Standalone environment/menu launch helpers |
| `key_probe.py` | Input-method diagnostic |
| `live_test_decision.py` | Live decision diagnostic; excluded from release |
| `threshold_sweep.py` | Detector threshold diagnostic |
| `visual_check.py`, `visual_check_elevated.vbs` | Visual capture diagnostics |
| `monitoring/monitor.py` | Basic monitoring tool |
| `monitoring/monitor_plus.py` | Extended monitoring tool |
| `monitoring/quick_status.py` | Quick monitoring status tool |
| `tools/check_optimization.py` | Optimization diagnostic |
| `tools/demo_mob_hunting.py` | Hunting demo |
| `tools/diagnose_errors.py` | Error diagnostic |
| `tools/test_mob_hunting.py` | Tool-level hunting test |
| `tools/test_monitor.py` | Tool-level monitor test |
| `weights/best.pt` | Tracked trained YOLO weights shipped in releases |
| `weights/.gitkeep` | Keeps the weights directory in Git |
| `.gitattributes`, `.gitignore` | Subproject Git settings |
| `README.md`, `GITHUB_SETUP.md` | YOLO subproject documentation |
| `docs/使用說明.md` | Usage documentation |
| `docs/快速開始指南.md` | Quick-start documentation |
| `docs/監控系統說明.md` | Monitoring documentation |

### Existing ignored/generated paths on the development machine

These exist locally but are not guaranteed in a clone or commit:

| Path | Contents/handling |
|---|---|
| `.venv/`, `yolo-detection/venv313/` | Local Python environments; never commit |
| `work/` | Logs, state JSON, debug captures, ad-hoc diagnostics, release gate log, and required local `verify_zip.py` |
| `release/` | Rebuilt distributable directory and timestamped ZIP |
| `recording-assets/` | Map-name index/reference images and map-structure reference; packaged when present |
| `outputs/` | Generated detection outputs |
| `map_profiles/` | Currently local/empty profile directory |
| `yolo-detection/weights/best.onnx` | Optional local ONNX weight; packaged when present, not currently tracked |
| `auto_system.log`, `yolo-detection/*.log` | Runtime logs |
| `__pycache__/`, `.idea/`, temporary directories | Generated development artifacts |

## 11. Change checklist for future sessions

1. Read `README.md` and this file before modifying behavior.
2. Inspect `git status --short`; preserve unrelated user changes.
3. Use logs to identify the state transition that actually fired.
4. Add a regression test reproducing the trace before or with the fix.
5. Run focused tests, then the relevant complete test modules.
6. Update this inventory when adding/removing/renaming files or changing
   ownership and data flow.
7. Run `release_now.ps1` without `-SkipTests`.
8. Verify the new ZIP, run `git diff --check`, and review the diff.
9. Commit intended tracked changes and confirm a clean worktree.
10. Hand off the behavior change, test count, ZIP path, commit hash, and any
    remaining operational caveat.
