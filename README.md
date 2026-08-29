# Maple Assistant

> The repository also contains the separately launched and separately packaged
> [BOSS 追踪](boss_tracker/README.md) companion application. It does not share
> Maple Assistant runtime state or automation workers.

Maple Assistant is a Windows desktop automation tool for a MapleStory client.
It captures the game window, reads the minimap and HP/MP bars, follows a
recorded multi-layer patrol route, climbs ropes, recovers from falls, collects
items. Fixed-interval attack remains available; YOLO-driven monster attack is
temporarily disabled until its model has been trained more reliably.

The runtime is resolution-adaptive: minimap geometry and the yellow player
diamond are detected dynamically, while normalized fallback regions keep the
assistant usable when detection confidence is low.

> Automation may violate a game or server's rules. Use it only where permitted.

## Start here

For a packaged release on a new Windows machine:

1. Extract the complete ZIP.
2. Double-click `安装.bat`. It finds or installs Python 3.10, creates `.venv`,
   installs the core dependencies through the Alibaba Cloud PyPI mirror and
   creates the launchers. If that mirror is unavailable, installation retries
   against official PyPI. The large YOLO dependencies are temporarily skipped.
3. Double-click `启动助手.bat`.
4. Bring the configured game window to the foreground.
5. Record or verify the patrol route in the UI, then click **Start Patrol**.

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

## What the assistant does

- `CaptureWorker` captures the client and publishes latest-only frames.
- `MinimapDetector`, `marker_detector.py`, and `MapStructureTracker` locate the
  minimap, yellow/red diamonds, and scroll-compensated map position.
- `MovementWorker` owns patrol movement, endpoint sequencing, stair jumps,
  rope approach/climbing, fall recovery, route return, and self-rescue.
- `StatusWorker` reads HP/MP and sends configured potion or buff keys.
- `AttackWorker` performs the optional fixed-rate attack mode.
- The UI can launch `yolo-detection/live_view.py` as a second process for
  target-aware attacks and main-screen rope sensing.
- `FocusWorker` releases held keys on a focus dip, tries to refocus the game,
  resumes after a short transient dip, and stops patrol after sustained loss.
- `ShutdownWorker` optionally stops the PC after a configured duration.
- `CountdownWorker` independently repeats an adjustable countdown, plays
  `sound/beep.mp3` at zero, and immediately resets for the next interval.
- The optional **掉线警报** consumes `CharacterWorker`'s existing yellow-marker
  result and plays the same beep after three consecutive missing frames.
- The optional **测谎报警** samples the shared full-client capture every one
  seconds and alarms when it finds a resolution-scaled pure-white square.
- The optional **闪烁提醒** adds two brief blue full-screen flashes to every
  countdown, disconnect, and lie-detector beep without affecting capture.

See [ARCHITECTURE.md](ARCHITECTURE.md) for worker wiring, state machines,
cross-process coordination, configuration ownership, and the complete file
inventory.

## Recording and patrol behavior

The shared route is stored in the `recording` section of `config.json`. In the UI, select
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

`patrol_cycles_per_layer` in `config.json` -> `rope_calibration` defaults to `2`. Every
layer completes two full horizontal Left/Right cycles before it climbs or,
for the top layer of the active route, drops back to the first route layer.
The final layer's rope is intentionally not used because no patrolled layer is
above it.

Adaptive recordings include `coordinate_v2` metadata based on minimap canvas
and diamond dimensions. Legacy ratio-only points still run, but should be
re-recorded when the UI labels them as a legacy layout.

## Movement and safety rules

- Only the movement worker owns directional movement and its paired pickup
  hold. Z is pressed and released with Left/Right during walking.
- Rope approach uses minimap X for navigation. Fresh YOLO rope/character boxes
  refine the jump direction and timing but do not control the route.
- Rope-stall recovery is armed only after repeated no-progress frames within
  the rope alignment range. While the rope is farther away, movement keeps
  walking across the platform and cannot start a recovery jump.
- Stair adaptation requires six consecutive no-progress frames (about 1.5
  seconds), so a normal fixed-attack animation does not trigger a stair jump.
- Climb arrival requires the expected next layer and stable confirmation. Up
  remains held through a short rope-top compensation window.
- Attack suppression ends immediately after confirmed climb arrival; the
  separate arrival grace period only prevents an unsafe stair jump.
- Fixed attack timing is `attack interval + random gap`; the additive random
  gap is uniformly selected from `0.0` through `0.1` seconds.
- Losing focus releases every key. After refocus, movement reconciles its
  internal hold state with the sender and re-presses externally released keys.
- A secondary character-marker reading can override movement only when it
  belongs to the same frame and detected minimap region; clipped startup
  readings such as `marker_y=0` are ignored.
- Patrol startup detects the character's actual recorded layer from the fresh
  adaptive minimap before anchoring world Y; it does not assume the character
  is already on the configured first patrol layer.
- A marker-only position verifier runs every 0.75 seconds using the existing
  movement observation. Two matching out-of-range readings clear stale
  climb/drop state and start return-to-route without another capture or image
  analysis pass.
- Self-rescue requires a genuinely stationary run. Off-layer readings use a
  separate consecutive counter, so adaptive minimap drift or visible patrol
  progress cannot cause a premature Alt+Down drop.
- Missing or stale cross-process state files mean “not busy,” preventing a dead
  process from permanently blocking patrol or attack.

## Configuration

All application settings now live in one ignored, persistent `config.json`.
Its sections are `recording`, `rope_calibration`, `drug`, `fixed_attack`,
`additional_functions`, `yolo_detection`, and `ui_window`.

On the first updated launch, `config_store.py` imports values from the old split
JSON files. After that, `config.json` is authoritative. Release ZIPs exclude
both `config.json` and the old split files, so extracting a new version over an
installation cannot overwrite the user's configuration. The lower-level YOLO
engine's developer-oriented `yolo-detection/config.yaml` remains separate.

Runtime state and logs are written under ignored directories such as `work/`,
`outputs/`, and `recording-assets/`. Do not assume these files are saved by Git.

## Temporarily disabled YOLO monster detection

YOLO monster detection is temporarily disabled because the current
`weights/best.pt` is not trained reliably enough. The implementation and model
files have deliberately not been deleted. While disabled:

- the UI forces **Fixed Attack**, greys the YOLO controls, and cannot launch
  the YOLO subprocess when patrol starts;
- fresh configuration defaults to `fixed` attack mode; and
- `install.ps1` skips the large PyTorch/Ultralytics dependency download. The
  preserved commands are inside the
  `YOLO_DEPENDENCIES_TEMPORARILY_DISABLED` block comment.

To recover YOLO quickly after replacing `yolo-detection/weights/best.pt` with a
better-trained model:

1. In `ui_worker.py`, change
   `_YOLO_MONSTER_DETECTION_ENABLED = False` to `True`.
2. In `install.ps1`, remove the opening
   `<# YOLO_DEPENDENCIES_TEMPORARILY_DISABLED` line and its matching closing
   `#>` line, then run `安装.bat` once to install the preserved dependencies.
3. Optionally change `config_store.py`'s default `fixed_attack.attack_mode`
   from `fixed` to `yolo`; otherwise select YOLO in the restored UI.
4. Update the temporary wording in `安装.bat`, `build_release.ps1`, this section,
   and `ARCHITECTURE.md`, then run the normal release workflow below.

The minimap-based patrol, layer, rope, fixed attack, potion, and alert workers
do not require these YOLO dependencies and continue to operate normally.

## Publishing a release

Every project update must produce a new release and a Git commit. Run the
canonical workflow from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1
```

`发布.bat` invokes the same script. A real release must not use `-SkipTests`.

The script performs three gated stages:

1. **Release-gate tests** using
   `yolo-detection\venv313\Scripts\python.exe` when present, otherwise
   `python`. Output is saved to `work\release_gate.log`; any failing test stops
   the release.
2. **Build and ZIP** via `build_release.ps1 -Zip`. The destination
   `release\MapleAssistant\` is rebuilt from scratch, runtime files and model
   weights are copied, ignored development/test artifacts are excluded, old
   ZIPs are removed, and a new `MapleAssistant-{dd-HH-mm}.zip` is created.
3. **ZIP verification** via `work\verify_zip.py`. A verification failure means
   the ZIP must not be shipped.

Important publishing details:

- `build_release.ps1` must remain UTF-8 with BOM for Windows PowerShell 5.1.
  `release_now.ps1` repairs the BOM automatically when needed.
- `work\verify_zip.py` is currently a required local helper inside a
  Git-ignored directory. Confirm it exists before publishing on a fresh clone.
- `release\` is Git-ignored. The ZIP is a delivery artifact, not part of the
  commit.
- `config.json` is user-owned and excluded from releases; never replace it
  during an update.
- A successful command prints `release ready: <absolute zip path>`.

After a successful release, save the source change:

```powershell
git diff --check
git status --short
git add -- <changed source, tests, and documentation>
git commit -m "describe the completed change"
git status --short
```

The final status should be clean. Record the release ZIP name and commit hash in
the session handoff.

## Development checks

Run a focused test first, then the relevant module suite. For movement work:

```powershell
py -3.10 -m unittest test_movement_worker
```

The canonical release gate is defined in `release_now.ps1`; do not maintain a
second handwritten test list in this README. Run the release script before
shipping even when focused tests already passed.

Stop a console run with `Ctrl+C`. Keep the game visible during calibration and
use `--dry-run` whenever input injection is not intended.

The **Additional Functions** panel includes an independent repeating sound
reminder. Set its interval in hours, enable it, and use the remaining-time bar
to move the current deadline anywhere from zero to the full interval. For
example, with a `1.0h` interval, dragging the bar to `20m 00s` makes the next
beep occur in 20 minutes. At zero, `sound/beep.mp3` plays and the bar resets to
the full interval. The timer does not depend on patrol or attack being active.

Selecting **掉线警报** reuses the normal per-frame yellow-character-diamond
detection. Three consecutive missing frames confirm the loss and play one
beep; the alarm re-arms after the marker is detected again. Audio runs in the
background, so it does not slow the character detector or add another capture.

Selecting **测谎报警** checks one in-memory full-game frame every second.
Its reference signature is a `40×40` block of entirely pure-white pixels in a
`1075×768` client. Both target dimensions scale independently with the current
client resolution. A visible match plays one beep; after the square disappears,
a later match can alert again. The detector never saves screenshots, so no used
image files remain to delete.

Selecting **闪烁提醒** makes the primary screen flash red for 0.5 seconds,
turn off for 0.3 seconds, then flash red for another 0.5 seconds whenever one
of the existing beep alarms fires: the repeating countdown, 掉线警报, or 测谎报警.
It is a separate notification worker and does not create another capture loop.
