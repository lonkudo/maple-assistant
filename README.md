# Maple Assistant

Maple Assistant is a Windows helper for a MapleStory client. It captures the
game window, detects the minimap and player marker, follows recorded multi-layer
routes, handles ropes and recovery, performs configured attacks, watches HP/MP,
and provides alerts and quick messages.

> Automation may violate a game or server's rules. Use it only where permitted.

The separately packaged [BOSS 追踪](boss_tracker/README.md) application is
independent and does not share Maple Assistant workers or configuration.

## Install and run

For a release ZIP on a new Windows machine:

1. Extract the complete ZIP to a writable folder.
2. Double-click `安装.bat` and accept its one UAC prompt. It finds or silently
   installs Python 3.10, creates `.venv`, and installs core packages through
   the Alibaba Cloud mirror with an official-PyPI fallback. YOLO dependencies
   are intentionally skipped.
3. Double-click `启动助手.bat`.
4. Record a route, choose the patrol range, then click **开始巡逻**.

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
- **小碎步** optionally makes an atomic left/right or right/left 150 ms pair.

`MotionArbiter` serializes action motions against attack. It permits queued
motions only during normal left/right patrol or movement toward a rope, waits
briefly after an attack, and lets the movement worker cleanly pause/resume its
normal walk hold.

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

Each discrete hotkey has a cooldown. Recording hotkeys refuse route changes
during patrol. The hook ignores assistant-generated input and does not steal
ordinary game typing. Every quick-message row is left-aligned and displays its
current **Ctrl+1 … Ctrl+0** index; deleting a row immediately shifts the later
indices. Buttons short-click to copy, double-click to focus the game and send
Enter → Ctrl+V → Enter, and long-press to edit.

## Optional alerts

Alert sources are **掉线**, **测谎**, and **循环**. Output choices are independent:
**声音** plays `sound/dingdong.mp3`, **闪烁** flashes red twice, and **消息**
sends Telegram with machine name, event type, and time.

- 掉线 reuses the yellow marker result and alerts after three missing frames.
- 测谎 scans the shared capture once per second for a resolution-scaled white
  square and saves no screenshots.
- 循环 is an independent draggable countdown; it does not depend on patrol.
- Telegram failures only change UI/log status and never stop the assistant.

## Configuration and updates

| File | Ownership | Purpose |
|---|---|---|
| `user_config.json` | user | route, minimap calibration, UI/drug/attack/alert/Telegram settings |
| `system_config.json` | release | internal movement and rope calibration defaults |
| `hotkey.json` | release | default Ctrl bindings |

`user_config.json` has a `user_config_updated_at` tag. The built-in updater
compares this before replacing a Desktop configuration: matching nonempty tags
preserve the installed user file. Do not overwrite user configuration during
ordinary development or updates. Legacy `config.json` and former split JSON
files are migration inputs only.

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
tests or a release ZIP. `release_now.ps1` advances `VERSION`, rebuilds
`release/MapleAssistant`, and produces `MapleAssistant-vNNNN.zip`. Version
`9999` never wraps.

## Important files

| File | Responsibility |
|---|---|
| `assistant.py` | application wiring and lifecycle |
| `ui_worker.py` | Tk UI, recording, settings, update/export actions |
| `movement_worker.py` | patrol, rope, fall/recovery, directional ownership |
| `motion_arbiter.py` | serialized jump/buff/small-step actions |
| `status_worker.py` | HP/MP bars, potions, timed drug/buff scheduling, shared key sender |
| `attack_worker.py`, `random_jump_worker.py` | attack and optional jump timing |
| `patrol_control.py` | route model and persistence |
| `minimap_detector.py`, `marker_detector.py`, `map_structure_tracker.py` | minimap geometry, marker detection, world-Y tracking |
| `config_store.py`, `update_manager.py` | configuration ownership and self-update |
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
