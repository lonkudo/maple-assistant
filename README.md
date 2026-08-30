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

Start Patrol first selects and verifies the game in the foreground, then opens
a temporary capture-only phase to collect stable minimap samples. Keyboard
input is armed only after that calibration succeeds. UI-overlaid pre-focus
frames therefore cannot enter the stability vote, and window checking remains
idle before Start Patrol.

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
  result and raises an alert after three consecutive missing frames.
- The optional **测谎警报** samples the shared full-client capture every one
  seconds and alarms when it finds a resolution-scaled pure-white square.
- The independent **声音提醒**, **闪烁提醒**, and **消息提醒** choices deliver
  every countdown, disconnect, and lie-detector event through beep audio, two
  red full-screen flashes, and Telegram respectively.
- The optional **消息提醒** queues those same events to a separate Telegram
  worker with a machine marker, event type, and local timestamp.

See [ARCHITECTURE.md](ARCHITECTURE.md) for worker wiring, state machines,
cross-process coordination, configuration ownership, and the complete file
inventory.

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

`patrol_cycles_per_layer` in `system_config.json` -> `rope_calibration` defaults to `2`. Every
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
- Stair adaptation requires ten consecutive no-progress frames (about 2.5
  seconds). Progress is measured cumulatively from a stable anchor, so several
  small but real movements reset the counter and attack animations do not
  trigger false stair jumps.
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
  is already on the configured first patrol layer.
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
- Missing or stale cross-process state files mean “not busy,” preventing a dead
  process from permanently blocking patrol or attack.

## Configuration

Configuration is intentionally split by ownership:

- `user_config.json` contains only UI-managed values and route recordings:
  `recording`, `drug`, `fixed_attack`, `additional_functions`,
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
2. **Advance version, build, and ZIP.** `VERSION` is a four-digit counter from
   `0000` through `9999`. A missing file starts at `0000`; every later
   successful `release_now.ps1` run advances it once. The value is restored if
   build or verification fails. `build_release.ps1` receives that version. The destination
   `release\MapleAssistant\` is rebuilt from scratch, runtime files and model
   weights are copied, ignored development/test artifacts are excluded, old
   ZIPs are removed, and `MapleAssistant-v{0000..9999}.zip` is created. The
   packaged `VERSION` file drives the visible `Maple 助手 (vNNNN)` UI label.
3. **ZIP verification** via `work\verify_zip.py`. A verification failure means
   the ZIP must not be shipped.

Important publishing details:

- `build_release.ps1` must remain UTF-8 with BOM for Windows PowerShell 5.1.
  `release_now.ps1` repairs the BOM automatically when needed.
- `work\verify_zip.py` is currently a required local helper inside a
  Git-ignored directory. Confirm it exists before publishing on a fresh clone.
- Version `9999` is terminal: publishing stops with an error instead of
  wrapping back to `0000`.
- `release\` is Git-ignored. The ZIP is a delivery artifact, not part of the
  commit.
- `user_config.json` is user-owned and excluded from releases; never replace it
  during an update. `system_config.json` must be included and replaced so
  internal fixes follow the application version.
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

The **Additional Functions** panel includes an independent **循环警报**. Set its
interval in hours, enable it, and use the remaining-time bar
to move the current deadline anywhere from zero to the full interval. For
example, with a `1.0h` interval, dragging the bar to `20m 00s` makes the next
event occur in 20 minutes. At zero, the selected reminder outputs run and the
bar resets to the full interval. The timer does not depend on patrol or attack
being active.

Selecting **掉线警报** reuses the normal per-frame yellow-character-diamond
detection. Three consecutive missing frames confirm the loss and raise one
event; the alarm re-arms after the marker is detected again.

Selecting **测谎警报** checks one in-memory full-game frame every second.
Its reference signature is a `40×40` block of entirely pure-white pixels in a
`1075×768` client. Both target dimensions scale independently with the current
client resolution. A visible match raises one event; after the square
disappears, a later match can alert again. The detector never saves
screenshots, so no used image files remain to delete.

The reminder row controls event delivery independently: **声音提醒** plays
`sound/beep.mp3`; **闪烁提醒** makes the primary screen flash red for 0.5
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
the Windows clipboard, long-press it for one second to edit it, and long-press
its adjacent `×` button for one second to delete it. Quick messages are stored
in the ignored `user_config.json` and survive application updates.
