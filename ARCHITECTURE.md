# Maple Assistant Architecture

## Design goals

Maple Assistant is a stateful Windows automation application. Its architecture favors a small number of shared truths over independent worker guesses:

- one focused game window;
- one shared capture cadence;
- one authoritative minimap geometry record;
- one input arbitration boundary;
- one durable user configuration;
- explicit ownership and release of every held key.

This keeps patrol recovery, combat, alerts, and optional helpers from fighting over the same keyboard state.

## System map

```text
                         ┌───────────────────────┐
                         │   Maple UI / Hotkeys   │
                         │ configuration + intent │
                         └───────────┬───────────┘
                                     │
                         ┌───────────▼───────────┐
                         │     assistant.py       │
                         │ lifecycle + wiring     │
                         └───────┬───────┬───────┘
                                 │       │
                  ┌──────────────▼───┐ ┌─▼─────────────────────┐
                  │ Shared capture    │ │ Window/input boundary │
                  │ 5 FPS game frames │ │ focus + key ownership │
                  └──────┬────────────┘ └─┬─────────────────────┘
                         │                │
       ┌─────────────────┼──────────────┐ │
       │                 │              │ │
┌──────▼──────┐  ┌───────▼────────┐ ┌──▼─▼───────────┐
│ Minimap and │  │ Status/character│ │ Movement and   │
│ map geometry│  │ event detection │ │ motion arbiter │
└──────┬──────┘  └───────┬────────┘ └──┬─────────────┘
       │                 │             │
       └──────────┬──────┴──────┬──────┘
                  │             │
       ┌──────────▼───┐   ┌─────▼─────────────────┐
       │ Patrol state │   │ Optional workers       │
       │ and recording│   │ attack, drugs, alerts │
       └──────────────┘   │ reconnect, auto lie    │
                          └────────────────────────┘
```

## Runtime ownership

### Application coordinator

`assistant.py` creates workers, supplies their callbacks, manages startup/shutdown, and owns the integration decisions that cross component boundaries. It also prepares the automatic-lie WebSocket endpoint in the background at application startup.

The coordinator is intentionally the only place where a capture source, a worker callback, and a UI action are connected together. Feature workers should remain independently understandable and should not reach across the application to manipulate unrelated state.

### Shared capture pipeline

`capture_worker.py` establishes the shared frame cadence: **5 FPS**. Consumers receive the same capture generation instead of each creating their own screen-grab loop. Frame-count thresholds are calibrated against this cadence, so time-based behavior stays consistent.

The capture path follows this order:

1. Select and verify the target game window when capture requires focus.
2. Capture one game frame.
3. Publish that frame and its generation timestamp.
4. Let vision and event consumers analyze it without performing another capture.

This shared model prevents conflicting focus changes, reduces capture overhead, and makes marker-loss and timeout counters meaningful.

### Minimap geometry and position

`minimap_detector.py` locates the actual minimap border and detects the yellow character marker. All normalized map coordinates are calculated from the detected border, not from a fixed screen region.

There are two distinct concepts:

- **Search region:** a broad area used to find a candidate minimap.
- **Verified geometry:** an OpenCV-confirmed border that is safe to record and use for normalized coordinates.

A broad search region is never promoted to saved geometry. This protects patrol calibration when the minimap changes size, the UI overlays the game, or a candidate search rectangle is larger than the map itself.

Recorded points contain minimap-relative X/Y values. Layer recognition uses recorded Y anchors and asymmetric layer bands: the top of a layer is more tolerant than its confirmed base. World-Y tracking provides continuity between frames and is re-anchored only by explicit recovery logic, not by ordinary movement such as stepping onto a bench.

Each layer may also own X-sorted, immutable directional jump points. A point records X, Y, and either left or right travel direction. During a horizontal leg, the direction must match the leg. During an active rope climb, either directional record may be selected if the marker matches its X/Y window. The jump-point matching policy is intentionally asymmetric: X is ±0.010, while Y is ±0.020 to accommodate vertical rope movement. A matched point is marked passed only after the dedicated jump worker accepts it.

### Patrol and movement

`movement_worker.py` owns the patrol state machine. It reads the latest character/map state, resolves the current recorded layer, and emits decisions such as patrol left/right, move-to-rope, climb, return-to-route, recovery, or safe wait.

Patrol has these core rules:

- Routes are an inclusive range of recorded layers, from bottom to top.
- The assistant can start on any layer. It either patrols that layer or follows the recorded route back into range.
- Crossing a recorded endpoint advances the route phase only after the intended direction reaches or passes its target.
- A missing route produces a safe wait state rather than accidental movement.
- Stand-still attack captures its temporary position at each manual patrol start and does not require recorded layer membership.

Movement never sends raw, competing direction requests. Every decision goes through the input boundary and, for atomic motions, the dedicated motion worker.

### Input, focus, and motion arbitration

`status_worker.py` supplies the target-window selection and key-sending abstraction. It tracks key ownership so a key is released only when its final owner is finished.

Horizontal directions are mutually exclusive with each other, as are vertical directions. A horizontal and vertical key may coexist for rope and jump-point chords. A horizontal direction switch follows this sequence:

1. release the active walk direction;
2. allow the brief handoff interval;
3. arm the new direction;
4. restore pickup ownership only when walking is active.

`motion_arbiter.py` handles finite, atomic motions: rope jumps, return motions, small steps, queued buffs, and stationary-facing taps. The dedicated stair-jump worker owns recorded jump-point taps: it presses the recorded horizontal direction with Alt and preserves Up until the movement worker observes a stable landing Y. Atomic workers return control to patrol after completion and are deliberately not used for ordinary continuous walking.

Attack and buff workers ask whether a conflicting motion is active before sending input. Movement does not wait while holding the arbiter’s internal lock; this avoids a stalled worker deadlock at a direction handoff.

### Combat and consumables

`attack_worker.py` performs fixed-interval attack and optional jump attack. `drug_worker.py` prioritizes HP/MP thresholds and runs pet food as a non-movement timed consumable. Small-step and stair-jump logic are isolated from ordinary patrol decisions so recovery behavior does not repeatedly enqueue the same input.

In stationary attack mode, the temporary anchor is captured only on a manual patrol start. Its X/Y recovery band is local to that anchor and never depends on a recorded layer. A requested stationary facing is queued only after a real recovery or a later setting change: startup deliberately records the current position without injecting a directional tap. 双向 changes the desired facing after 80 settled observations; the arbiter performs one short facing tap rather than a walking movement.

Stair jump observes sustained position stalls and submits one direction-preserving recovery jump. It uses a post-trigger frame cooldown and a movement-progress reset, preventing a single long stall from producing a burst of jumps. Recorded jump points are separate from this recovery rule: they are deliberate X/Y triggers, have their own once-per-leg pass record, and can fire while a rope climb is active.

### Alerts, disconnects, and reconnect

`character_worker.py` identifies marker loss, disconnect candidates, other-player conditions, and related alerts. A marker absence alone is not a disconnect:

1. the yellow marker must be absent for 50 shared frames;
2. the same frame must pass the login-page check supplied by `reconnect_worker.py`, or match its dedicated offline-prompt square;
3. only then may disconnect alerting, patrol stop, and reconnect begin.

If the absence threshold occurs without either offline evidence — for example in a minimap-less zone — the worker enters a quiet paused state. It resets and resumes normally when the marker returns. `reconnect_worker.py` owns the prompt dismissal, login-page interaction, and confirmation clicks; its frame-only evidence checks are also used for the disconnect gate.

### Automatic lie handling

`api_lie_test.py` is the local client adapter. `autolie_api/` is vendor reference material and must remain unchanged.

At UI/application startup, `assistant.py` performs an inexpensive WebSocket endpoint probe and caches the usable endpoint. When a lie event occurs, the adapter uses that cached endpoint to create a fresh authenticated connection while the game completes its visual transition. The event timeline is:

```text
event start ── connect/setup in parallel ── 3 s visual settle
            └──────────────────────────────► frame uploads, at most 13 s
                                                └► round_end + WebSocket close
```

The live frame stream uses the shared capture cadence. The temporary detection rectangle and one-shot target marker are display-only overlays and must never become tracking input.

### UI, configuration, and optional tools

`ui_worker.py` presents the Chinese desktop interface. It reads and writes only through configuration callbacks supplied by the coordinator. UI redraw work is deferred during resize/drag operations to avoid black component flashes and expensive intermediate layouts. `screen_blinker.py` owns click-through diagnostic overlays: crosshairs and all patrol-point symbols are painted on persistent native canvases, never into capture input.

Personal settings are stored in `user_config.json`; application defaults are stored in `system_config.json`. Runtime scratch data belongs under `work/`, including patrol state and timer persistence. Critical exceptions additionally go to `error.log`.

`hotkey_worker.py`, quick messages, trade helpers, Telegram notification, screen blinking, countdowns, and shutdown logic are optional subsystems. They use isolated configuration/state and may request input only through the shared focus/key boundary.

## Data and persistence

| Location | Owner | Contents |
| --- | --- | --- |
| `user_config.json` | UI/config callbacks | User choices, keys, patrol layers, message list, alerts. |
| `system_config.json` | Application defaults | Internal defaults and non-personal settings. |
| `hotkey.json` | Hotkey worker | Ordered hotkey bindings. |
| `work/` | Runtime workers | Recoverable patrol/timer/session state. |
| `error.log` | Error reporting | Critical unexpected-error record. |
| `autolie_api/` | Vendor | Reference protocol implementation; read-only. |

Atomic file replacement is used for runtime state where possible. A permission failure while writing a runtime file must be reported and must not leave held input active.

## Concurrency rules

1. **Capture is shared.** Do not add an independent high-frequency capture loop without coordinating it with `capture_worker.py`.
2. **Directions are serialized.** Only the input boundary can change held left/right/up/down state.
3. **Finite motions are atomic.** Queue finite corrective motions through the arbiter; do not use the arbiter for routine walking.
4. **Focus is a guard, not a background loop.** Window verification happens before an action that needs the game, not as continuous UI-stealing polling.
5. **Workers communicate intent, not raw state mutations.** A worker may request a motion or alert; it must not independently overwrite another worker’s session state.
6. **Release is mandatory.** Stop, focus loss, and worker failure must clear every held key through the shared sender.
7. **Vendor boundaries remain intact.** Extend auto-lie behavior in the adapter layer, never by editing `autolie_api/`.

## Release discipline

`VERSION` is the release source of truth. A behavior change is packaged with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

The script creates the single `release/MapleAssistant-<version>.zip` package and removes the prior ZIP. Field runs are the normal verification path. Do not run broad unit-test suites unless the operator specifically requests them.

For a component-level guide, start with the source modules named above, then follow their constructor wiring in `assistant.py`. This preserves the intended direction of dependency: UI and workers depend on coordinator-provided interfaces; they do not depend directly on one another.
