# TodoHelper Architecture

## Design goals

TodoHelper is a stateful Windows automation application. Its architecture favors a small number of shared truths over independent worker guesses:

- one focused game window;
- one shared capture cadence;
- one authoritative minimap geometry record;
- one input arbitration boundary;
- one durable user configuration;
- explicit ownership and release of every held key.
- local signature verification separated from server-side activation authority.

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

### Licensing and online-service boundary

`licensing.py` is the local authorization boundary: it verifies signed license
documents using the public key packaged with the desktop application and keeps
the automation gate non-throwing. The latest submitted activation code and its
safe validation result live in the user-owned `license` configuration section;
the signed `license.json` is the entitlement authority. A verified rejected
replacement code removes that entitlement and locks every automation worker,
whereas transport/TLS/server-availability failures remain distinguishable and
do not erase a previous entitlement. For a `MAL-` activation code, it derives
an opaque stable fingerprint locally and calls the fixed activation API at
`https://211.149.169.194:8443/api/v1/activate`. The customer never supplies an
endpoint. A successful v2 response is Ed25519-verified, checked against the
local fingerprint, and atomically saved as `license.json`.

The activation server is a separate Django repository and owns code issuance,
device binding, expiry, bans, the PostgreSQL database, and private signing
material. `LICENSE_SIGNING_PRIVATE_KEY` is an environment-only Base64url
Ed25519 private key. Its matching public key is the `license_public_key.json`
file shipped with the desktop release. The desktop repository must never
contain the server database, plaintext activation inventory, signing private
key, TLS private key, operator token, or auto-lie product secret. Its binding
policy is one code to one fingerprint; a fingerprint may activate multiple
different codes over time.

`pinned_tls.py` is a transport-only component. It reads the public pin from
`activation_server_pin.json`, checks the server TLS public-key hash before a
request body is sent, and never falls back to plain HTTP. Safe milestones are
written to the dedicated `server-client` logger, which `assistant.py` persists
as `server_client.log`. This log records no activation code, fingerprint,
token, or secret. It does not alter the existing auto-lie WebSocket adapter. A
future online-session component will obtain a short-lived in-memory product key
only after server authentication; that component must remain separate from both
license verification and vendor `autolie_api/` code.

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

Each layer may also own X-sorted, immutable directional jump points. A point records X, Y, and either left or right travel direction. During a horizontal leg, the direction must match the leg. During an active rope climb, either directional record may be selected if the marker matches its X/Y window: the recorded direction is only a preference there (it breaks a tie when a 左跳 and a 右跳 sit at the same height), because a climb has no horizontal leg to compare against. Gating a rope point on the direction the character happened to *walk in* to the rope made a point recorded as a right jump invisible during a return climb that approached from the right — the rope jump point that never fired. The matching policy is intentionally asymmetric: X is ±0.010, while Y is ±0.020 to accommodate vertical rope movement. A matched point is marked passed only after the dedicated jump worker accepts it.

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

`motion_arbiter.py` handles finite, atomic motions: rope jumps, return motions, small steps, queued buffs, stationary-facing taps, and the 站桩 anchor correction. The dedicated stair-jump worker owns recorded jump-point taps: it presses the recorded horizontal direction with Alt and preserves Up until the movement worker observes a stable landing Y. Atomic workers return control to patrol after completion and are deliberately not used for ordinary continuous walking.

The anchor correction is the one motion that also carries an attack (see the combat section). It is exclusive in the usual way — while it is queued or running, the fixed cadence cannot reserve a beat, so the correction’s own attack can never be doubled — but it is exempt from the arbiter’s shared post-attack grace, because that grace is measured from the correction’s own attack and would stretch a correction the operator expects roughly every 300 ms into one per second.

While the optional 小碎步 pair is queued or running the movement loop queues nothing behind it: neither an anchor correction nor a 朝向 tap. The pair moves the character on purpose (away from, then back to the selected 朝向) and records the facing itself when it completes, so anything queued during it executed as an extra step straight after the pair. The facing obligation stays owed, so a pair that is dropped is corrected on the next settled capture.

Attack and buff workers ask whether a conflicting motion is active before sending input. Movement does not wait while holding the arbiter’s internal lock; this avoids a stalled worker deadlock at a direction handoff.

### Combat and consumables

`attack_worker.py` performs fixed-interval attack and optional jump attack. `drug_worker.py` prioritizes HP/MP thresholds and runs pet food as a non-movement timed consumable. Small-step and stair-jump logic are isolated from ordinary patrol decisions so recovery behavior does not repeatedly enqueue the same input.

In stationary attack mode, the temporary anchor is captured only on a manual patrol start. Its X/Y recovery band is local to that anchor and never depends on a recorded layer. A requested stationary facing is queued only after a real recovery or a later setting change: startup deliberately records the current position without injecting a directional tap. 双向 changes the desired facing after 80 settled observations; the arbiter performs one short facing tap rather than a walking movement.

That facing tap is a 30 ms turn, deliberately as short as the tiny anchor step: it exists to turn the character, and a longer hold walks it out of the anchor band, after which the correction walks it back, turns it again and re-arms the facing — the face/walk twitch.

The anchor correction is one atomic motion per correction, shaped as “direction hold, then the attack belonging to that correction”. The hold is the tiny step inside the small-step band (±0.010X of the anchor) and the longer walk when the character is further out; both carry exactly one attack. The design reason is direct field evidence: as an ordinary walk hold the correction either deferred the fixed cadence (direction handoff) or blocked it (exclusive recovery), so the character corrected its position and attacked nothing. Corrections are spaced about 300 ms apart and repeat until the marker is inside the arrival band (±0.006X). That band is not widened to “hold” a small drift: a position that is off the band is always walked back, because tolerating it left the character standing a pixel or two away from its 桩.

Stair jump observes sustained position stalls and submits one direction-preserving recovery jump. It uses a post-trigger frame cooldown and a movement-progress reset, preventing a single long stall from producing a burst of jumps. Recorded jump points are separate from this recovery rule: they are deliberate X/Y triggers, have their own once-per-leg pass record, and can fire while a rope climb is active.

### Alerts, disconnects, and reconnect

`character_worker.py` identifies marker loss, disconnect candidates, other-player conditions, and related alerts. A marker absence alone is not a disconnect:

1. the yellow marker must be absent for 50 shared frames;
2. the same frame must pass the login-page check supplied by `reconnect_worker.py`, or match its dedicated offline-prompt square;
3. only then may disconnect alerting, patrol stop, and reconnect begin.

If the absence threshold occurs without either offline evidence — for example in a minimap-less zone — the worker enters a quiet paused state. It resets and resumes normally when the marker returns. `reconnect_worker.py` owns the prompt dismissal, login-page interaction, world/channel selection, and confirmation clicks; its frame-only evidence checks are also used for the disconnect gate.

World selection is intentionally its own verified gesture. `reconnect_worker.py` sends a double-click to the configured world row, waits 0.5 seconds, checks the page again, and only sends the second double-click if the world page remains visible. A transition to the channel page after the first double-click ends the step immediately; the second gesture must never land on a channel row. This path has no single-click or Enter fallback, because either can confirm the wrong/default world.

The reconnect-only drop-recovery policy belongs to `movement_worker.py`, but is armed by the reconnect restart path in `assistant.py`. It remains inert for a manual patrol start and for a reconnect that begins within the route. After three genuine down-jump attempts without enough positive Y progress, it probes left for five seconds, then right for five seconds only if left did not reveal a drop. A successful descent returns control to normal drop-to-route movement.

`auto_restart_worker.py` is a distinct memory-leak recovery lifecycle. It samples Windows system memory every ten minutes and publishes that reading to the authorization header whether or not restart is enabled. At or above 95% with 自动重开 selected, it stops patrol, sends the optional 重开消息, signs out, terminates the game process tree, and waits without a fixed abandonment timeout until the old game window and process tree are gone. It then focuses the persistent `launcher3.0` dialog and clicks its measured launch point. A newly visible game window is not enough to start reconnect: the worker re-focuses it once per second while waiting for `ReconnectWorker`'s existing login-page colour evidence. Only a confirmed login page receives the dedicated reconnect handoff. If the fresh game remains visible but login/reconnect cannot complete, it is retained rather than killed into a restart loop. Auto-restart reports its lifecycle through the 图层校准与逻辑 status line; the Additional Functions panel contains controls only.

### Automatic lie handling

`api_lie_test.py` is the local client adapter. `autolie_api/` is vendor reference material and must remain unchanged.

At UI/application startup, `assistant.py` performs an inexpensive WebSocket endpoint probe and caches the usable endpoint. When a lie event occurs, the adapter uses that cached endpoint to create a fresh authenticated connection while the game completes its visual transition. The event timeline is:

```text
event start ── connect/setup in parallel ── 3 s visual settle
            └──────────────────────────────► frame uploads, at most 13 s
                                                └► round_end + WebSocket close
```

The live frame stream uses the shared capture cadence. The temporary detection rectangle and one-shot target marker are display-only overlays and must never become tracking input.

When the pass ends, `ui_worker.py` focuses the game and uses `click_screen()` to press the lie dialog's measured confirmation point. The point is `(630, 428)` on a 1080×768 client; all other clients derive from `(800, 472)` in the 1366×768 reference layout by width scaling. This post-pass acknowledgement is separate from WebSocket completion and must complete (or safely refuse when focus is unavailable) before patrol is resumed.

### UI, configuration, and optional tools

`ui_worker.py` presents the Chinese desktop interface. It reads and writes only through configuration callbacks supplied by the coordinator. UI redraw work is deferred during resize/drag operations to avoid black component flashes and expensive intermediate layouts. `screen_blinker.py` owns click-through diagnostic overlays: crosshairs and all patrol-point symbols are painted on persistent native canvases, never into capture input.

Each recorded layer row carries an axis band whose point menu (添加最左 / 添加绳索 / 添加最右 / 添加左跳 / 添加右跳) opens only on a **right click**. Left-clicking never opens the menu; it remains reserved for normal selection behavior. The menu itself is refused while patrol runs, because recording is locked then.

One optional control deliberately uses its own unit: the 捡东西 (stand-still pickup circuit) trigger interval is set in **minutes** (`每 … m`, 2.0m to 30.0m). The row converts before it publishes and after it loads, so `stationary_pickup_interval_seconds` and the movement worker’s own bounds stay in seconds; the interval is clamped to the same two minutes at the bottom and thirty at the top in both places, so a hand-edited configuration cannot schedule a sweep every few seconds. The random gap in that row remains a seconds control.

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
| `server_client.log` | Licensing transport | Safe activation connection, pin, HTTP, and local verification events; never secrets. |
| `assistant-launch-error.log` | `startup_probe.py` | Hidden-launch failures, including the missing dependency that triggered the automatic install. |
| `assistant-launch-status.log` | `startup_probe.py` | Launcher milestones: probe reached, repair started/finished, normal exit. |
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

## Packaging and release surfaces

Two packages are built from the same staged sources, and both must behave the
same way for the operator:

- `release/TodoHelper-<version>.zip` — the normal package: Python sources
  plus data, installed into `.venv` by `安装.bat`. `startup_probe.py` owns its
  hidden-launch failure path: it records the traceback to
  `assistant-launch-error.log`/`error.log`, and when the failure is a
  `ModuleNotFoundError` for a *third-party* module it runs `安装.bat` once and
  restarts the assistant in a fresh process (guarded by an environment flag, so
  it cannot loop). A missing local module is a damaged package, not an
  installable requirement, and is only reported.
- `release/TodoHelper-release-<version>.zip` — the standalone package built
  by `build_protected_release.ps1`: the same staged layout with the Python
  sources compiled by Nuitka into `TodoHelper.exe`. It contains no source,
  so `安装.bat` is absent and its launcher is generated from code points rather
  than Chinese literals.

Four rules keep the standalone package from drifting away from the normal one:

1. **Data parity is mechanical.** The build mirrors every non-source file of the
   staged normal package 1:1 instead of carrying a hand-written include list. A
   partial list is how `hotkey.json`, `system_config.json` and `timer.json`
   disappeared once, and the packaged app then started with
   `hotkey config unavailable` and no hotkey bindings.
2. **Dynamic imports must be named.** Nuitka sees only static `import`
   statements; `importlib.import_module("mouse_aim_controller")` in
   `api_lie_video.py` is invisible to it and is therefore passed explicitly with
   `--include-module`. Any new dynamic import needs the same treatment.
3. **The build must prove the result.** After compiling, the build starts the
   packaged executable once and refuses to publish when it cannot import a
   bundled module. Imports happen before the single-instance guard, so an
   immediate clean exit means "another instance is running", not a failure.
4. **Launcher behaviour is part of the package.** The standalone launcher is a
   hidden `wscript` that starts the executable with the `runas` verb, matching
   the normal package: at a different privilege level from the game, Windows
   silently drops injected keys. The executable is compiled with
   `--windows-console-mode=hide`, so no console window appears, while a console
   launched by the operator (the diagnostic launcher) still shows output.

The frozen package cannot host a feature that starts a *separate* Python
interpreter: `ui_worker.py` launches `yolo-detection/live_view.py` through
`sys.executable`, which inside the executable is the assistant itself. Such a
feature needs a bundled interpreter or must be compiled in; the YOLO panel is
hidden meanwhile. External binaries (ffmpeg in `lie_video_tools.py`) are
unaffected.

Operator-facing text rule: status and refusal hints are Chinese, including
every reason printed under **图层校准与巡逻**, while text that only reaches a log
file or a developer stays English so a traceback keeps its searchable wording.

## Release discipline

`VERSION` is the release source of truth. A behavior change is packaged with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

The script creates the `release/TodoHelper-<version>.zip` package and removes
the prior ZIP. The standalone package is built from that staged package
afterwards with the same version:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_protected_release.ps1 -Version X.Y.Z
```

The order matters: `release_now.ps1` prunes `TodoHelper-*.zip`, which also
matches the standalone package name. Field runs are the normal verification
path. Do not run broad unit-test suites unless the operator specifically requests
them.

For a component-level guide, start with the source modules named above, then follow their constructor wiring in `assistant.py`. This preserves the intended direction of dependency: UI and workers depend on coordinator-provided interfaces; they do not depend directly on one another.
