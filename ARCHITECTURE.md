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

Fingerprint collection has its own explicit failure category. Immediately
after Windows boot, CIM/WMI firmware or motherboard providers may not yet be
ready; fewer than two stable signals returns **设备未就绪，稍后重试**. A failed
pinned server request returns **激活验证失败** instead. This prevents a local
startup race from being presented as a server outage while keeping raw device
values out of logs.

The activation server is a separate Django repository and owns code issuance,
device binding, expiry, bans, the PostgreSQL database, and private signing
material. `LICENSE_SIGNING_PRIVATE_KEY` is an environment-only Base64url
Ed25519 private key. Its matching public key is the `license_public_key.json`
file shipped with the desktop release. The desktop repository must never
contain the server database, plaintext activation inventory, signing private
key, TLS private key, operator token, or auto-lie product secret. Its binding
policy is one code to one fingerprint and one *current* code per equipment:
activating a new code detaches the earlier binding, while that earlier code keeps
its expiry and its own remaining auto-lie balance for a later rebind. The
remaining balance is code-owned and the device column the desktop is shown is
only the aggregate of the equipment's active codes.

`pinned_tls.py` is a transport-only component. It reads the public pin from
`activation_server_pin.json`, checks the server TLS public-key hash before a
request body is sent, and never falls back to plain HTTP. Safe milestones are
written to the dedicated `server-client` logger, which `assistant.py` persists
as `server_client.log`. This log records no activation code, fingerprint,
token, or secret.

`auto_lie_secret.py` is the in-process credential boundary. A successful
activation or three-hour license heartbeat may contain an optional auto-lie
credential. `licensing.py` hands it to this module only after the pinned TLS
request and signed license response have been accepted; the module keeps it in
memory and replaces or clears it atomically. `assistant.py` reads this runtime
value only while opening the auto-lie WebSocket. No configuration, local
license, client log, or release package stores it. The server encrypts its
operator-managed copy at rest in PostgreSQL. This path is additive to the
versioned activation response so older clients can safely ignore it, and it
does not modify vendor `autolie_api/` code.

### Asynchronous auto-lie lifecycle accounting

`lie_accounting.py` is a durable accounting boundary, never part of the live
WebSocket/cursor stream. Once the authenticated WebSocket handshake is ready,
the adapter persists a UUID locally and asynchronously queues a `STARTED`
report with a random 0–30 second spread. The upload stream never waits for that
request. After the 15-second tracking window, marker recovery queues `success`;
only a verified black-room marker loss queues `failed_marker_missing`. Terminal
reports use their own 0–30 second spread, but are never scheduled before
`STARTED` has been acknowledged.

The outbox makes retries idempotent by UUID. A normal close queues success for
unfinished events, gives the sender a bounded 500 ms chance, and preserves any
unsent update for next launch. A forced termination cannot run client code;
the server therefore settles only rows older than two minutes at the same
device's heartbeat, and rows older than ten minutes in one indexed periodic
cleanup. Both windows use server `recorded_at`, never the client occurrence
time, so a newly received start cannot be inferred complete while its delayed
terminal report is still expected. Each event is settled independently; the
cleanup is one server job, not one timer per client.

Two details keep the outbox honest across versions and outages. A transport
failure (`server` or `device_not_ready`) is not a verdict: the row is kept and
its due time moves 60 s out, and a start that has not been accepted yet blocks
its own final report — the ordering rule is enforced by the data, not by the
caller's timing. A row persisted by a pre-lifecycle release (a completed event
with no `started_due_at`) is migrated on load into a start followed by its prior
`success`, so an update cannot orphan an older pending event.

The terminal outcome is evidence-based, and the failure evidence belongs to the
UI layer because the marker is read there: `ui_worker.py` arms a verification
window when the pass worker reports a result with an accounting event, ignores
the first `_AUTO_LIE_FAILURE_GRACE_SECONDS` (3 s) so a normal transition is not
counted, queues `success` on the first fresh snapshot that has a marker
(`marker_confidence > 0`), and queues `failed_marker_missing` only after
`_AUTO_LIE_FAILURE_MISSING_FRAMES` (15) consecutive fresh snapshots without one
— the black-room state. An interrupted pass (Esc, application close) is not a
failure and stays pending for the server's own settlement. The manual **测试API**
drill is an operator-run check of the same upstream service, so it keeps the
immediate begin-and-finalize-`success` accounting instead of the live lifecycle.

The server keeps its established `device` response for compatibility and adds
`autolie_fingerprint_usage` containing only current auto-lie permission,
remaining balance, and usage counters. `licensing._with_server_device()`
merges that additive object into the live `LicenseStatus`; `UiWorker` applies
it on its Tk thread and redraws the always-visible 设备码/usage line immediately.
Routine local license checks retain this online state for the same license,
rather than erasing heartbeat data that local signed documents cannot contain.

That merge deliberately resolves the two representations of the balance in one
order: a numeric `remaining_auto_lie_count` in the response wins outright, the
count is cleared only when the response carries the key with a null value, and
`remaining_auto_lie_unlimited` is believed only together with that null. The
desktop state therefore cannot hold a finite balance and the unlimited marker at
once, which previously let a stale `true` flag render a genuinely limited grant
as 无限 until the next restart; `UiWorker._render_lie_usage()` prints the count
before the marker for the same reason. The authorization caption treats
`device_not_ready` as a purely local device-readiness state, so it shows that
message alone instead of pairing it with 设备：等待服务器返回, and clicking the
device code renders （已复制） for 1.5 s before re-rendering from the authoritative
status — a heartbeat that arrives during that second is not overwritten by stale
caption text.

### Unified license edition and device notes

Licensing no longer has NORMAL/NP product behavior. New server-issued records
use the single compatible `normal` wire value, while the desktop accepts older
signed NORMAL/NP values without exposing an edition choice or enforcing a
feature split. This keeps previously issued documents valid while making all
future activation and feature gates identical.

The operator console maps an activation code's stored fingerprint binding to
the corresponding opaque `equipment_id` before displaying it. Raw fingerprint
hashes stay off the operator-facing code table. `FingerprintSecurityState.memo`
is an operator-only, bounded text field; device actions can set, replace, or
clear it, and it is never returned to clients.

### Server deployment compatibility boundary

The desktop application and `maple-assistant-server` are independently
deployable only across their stable, versioned API contract. A compatible
server replacement retains the TLS private key (therefore the client pin),
`LICENSE_SIGNING_PRIVATE_KEY`, PostgreSQL database, `/api/v1/activate`, and
`/api/v1/validate`. Response additions are optional; removals, renamed fields,
new mandatory fields, signing-key rotation, or TLS-key rotation require a
coordinated desktop release.

On a server failure, restoring the previous compatible server code against the
same database and keys restores existing clients. Online validation runs
immediately at desktop launch, so a newly started client locks while the server
is unavailable. A client already running from a successful validation stays
active only until its next three-hour heartbeat; that failed validation locks
automation. Server schema updates should be additive and code rollbacks should
remain compatible with the migrated schema rather than attempting a destructive
database downgrade.

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

There are three distinct concepts:

- **Search region:** a broad area used to find a candidate minimap.
- **Verified geometry:** an OpenCV-confirmed border that is safe to record and use for normalized coordinates.
- **Measured canvas:** the map panel's own inner rectangle inside that border. Whenever it can be measured (or is recalled from earlier in the session) it is the marker/patrol analysis region and the yellow diagnostic rectangle; when it cannot be, no canvas is claimed at all.

A broad search region is never promoted to saved geometry. This protects patrol calibration when the minimap changes size, the UI overlays the game, or a candidate search rectangle is larger than the map itself.

The canvas is measured from the captured pixels, not derived from the window: `_measure_inner_canvas()` takes the frame's detected window, builds its border contour tree, and accepts the largest rectangle nested directly inside the window's border that is inset on every side and covers a plausible share of the window. A fixed height, a fixed bottom offset, and a proportion of the window are all deliberately rejected — the game does not rescale this HUD uniformly between client widths, so a proportion that happens to fit one client is wrong on the next. Rectangles nested deeper than the window's border are map artwork or a panel drawn inside the canvas and are never adopted, because one of them would cut the character marker out of the analysis region. Two further passes (a fainter Canny threshold, then a gap-bridged edge map) cover a faint border and a border broken by map artwork, and the plain pass is preferred because closing an edge map can merge the canvas with the artwork and widen the box.

The resolution order is: the separate inner-border detector's measurement (including its held previous result) → an inner contour the outer pass already found → this pixel measurement → the canvas measured earlier in the same session, held through a few pixels of jitter so the coordinate frame does not move under a standing character, and cleared by `reset_geometry()` with the rest of the session geometry → and otherwise no canvas at all. That last case is deliberately honest rather than generous: `MinimapDetection.canvas_measured` is false, `assistant.py` flashes only the minimap frame, and it logs that no yellow marker/patrol region was drawn. A rectangle that claims the whole minimap window (or the broad search region) as the map canvas is worse than no rectangle.

Recorded points contain minimap-relative X/Y values. Layer recognition uses recorded Y anchors and asymmetric layer bands: the top of a layer is more tolerant than its confirmed base. World-Y tracking provides continuity between frames and is re-anchored only by explicit recovery logic, not by ordinary movement such as stepping onto a bench.

Each layer may also own X-sorted, immutable directional jump points. A point records X, Y, and either left or right travel direction. During a horizontal leg, the direction must match the leg. During an active rope climb, either directional record may be selected if the marker matches its X/Y window: the recorded direction is only a preference there (it breaks a tie when a 左跳 and a 右跳 sit at the same height), because a climb has no horizontal leg to compare against. Gating a rope point on the direction the character happened to *walk in* to the rope made a point recorded as a right jump invisible during a return climb that approached from the right — the rope jump point that never fired. The matching policy is intentionally asymmetric: X is ±0.010, while Y is ±0.020 to accommodate vertical rope movement. A matched point is marked passed only after the dedicated jump worker accepts it.

### Patrol and movement

`movement_worker.py` owns the patrol state machine. It reads the latest character/map state, resolves the current recorded layer, and emits decisions such as patrol left/right, move-to-rope, climb, return-to-route, recovery, or safe wait.

Patrol has these core rules:

- Routes are an inclusive range of recorded layers, from bottom to top.
- The assistant can start on any layer. It either patrols that layer or follows the recorded route back into range.
- Crossing a recorded endpoint advances the route phase only after the intended direction reaches or passes its target.
- A return drop ends on landing evidence read from the marker, never on the world-Y tracker: the marker's own recorded band, or — when the landing is away from the recorded row and therefore matches no band — the at/below-bottom-floor and bounded nearest-floor answers ordinary patrol already uses. Both relaxed answers require an Alt+Down chord to have really moved the marker down and the reading to hold for two consecutive settled frames, so a character falling *through* a floor is never read as landed. The world-Y tracker is excluded here because at a fresh start above the route its origin is anchored to the route's top floor while the character is still above it.
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

`motion_arbiter.py` handles finite, atomic motions: rope jumps, return motions, small steps, queued buffs, stationary-facing taps, the 站桩 anchor correction, and the **被撞反击** hit reaction. The dedicated stair-jump worker owns recorded jump-point taps: it presses the recorded horizontal direction with Alt and preserves Up until the movement worker observes a stable landing Y. Atomic workers return control to patrol after completion and are deliberately not used for ordinary continuous walking.

Patrol hit reactions are a motion type of their own (`COUNTERATTACK`) rather than a walk beside the cadence, so the short recovery hold and its bound counterattack key cannot overlap a jump, buff, climb, or ordinary attack. `request_counterattack()` refuses to stack, and the arbiter carries the optional hit X in its own token (`COUNTERATTACK:<direction>:<key>[:<hit_x>]`), so the reaction that finally executes still knows where the hit happened. For a clear knockback the direction is retained. For a walking-compensated hit (`abs(ΔX) < 0.003`) the token carries the marker X captured at the HP-drop frame, and `perform_counterattack()` re-derives the direction from the live marker against that saved X when it executes; the planned-walk opposite direction is the fallback, and a reaction whose marker has not moved a whole minimap column is sent as `direction="none"`, which taps the key without stepping. The reaction holds for `COUNTERATTACK_STEP_HOLD_SECONDS` (0.10 s), taps the independent key, then calls `note_attack()`.

Three details keep one hit equal to one reaction. The cooldown is claimed where the hit is *decided* (`COUNTERATTACK_COOLDOWN_FRAMES` = 3 frames), not where the arbiter happens to run it, so a damage animation that lowers HP across adjacent reads cannot queue a row of delayed reactions. The reaction is resolved immediately from marker samples the movement worker has already published, with `COUNTERATTACK_MARKER_SEQUENCE_TOLERANCE` (2 captures) absorbing the fact that HP parsing and marker detection finish at slightly different moments; only a hit whose sample has genuinely not been published yet stays queued for the next movement tick. And when the marker moved less than 0.003, `COUNTERATTACK_IMPACT_LOOKBACK_FRAMES` (4 captures) is searched for motion *against* the planned patrol direction, which is the only surviving evidence of a knockback the character's own walk cancelled.

The 站桩 half of the reaction owns no movement at all. `attack_slot.py` is a thread-safe, one-use key replacement created by the coordinator and handed to both sides: `AttackWorker` consumes it for the next cadence beat, and `MovementWorker._consume_attack_slot()` consumes it for the anchor correction and for 小碎步's middle attack. A stationary hit therefore only calls `replace_next()`, and whichever approved beat was going to happen anyway spends the counterattack key instead of the default one; an unconsumed replacement is retained rather than overwritten. This is what removed the competing movement transaction that used to fight the anchor correction for the same hit.

The anchor correction is the one motion that also carries an attack (see the combat section). It is exclusive in the usual way — while it is queued or running, the fixed cadence cannot reserve a beat, so the correction’s own attack can never be doubled — but it is exempt from the arbiter’s shared post-attack grace, because that grace is measured from the correction’s own attack and would stretch a correction the operator expects roughly every 300 ms into one per second.

While the optional 小碎步 pair is queued or running the movement loop queues nothing behind it: neither an anchor correction nor a 朝向 tap. The pair moves the character on purpose (away from, then back to the selected 朝向) and records the facing itself when it completes, so anything queued during it executed as an extra step straight after the pair. The facing obligation stays owed, so a pair that is dropped is corrected on the next settled capture.

Attack and buff workers ask whether a conflicting motion is active before sending input. Movement does not wait while holding the arbiter’s internal lock; this avoids a stalled worker deadlock at a direction handoff.

The initial long stationary-return walk has one additional, bounded gate. If an
attack is already inside the game-side animation window when the marker first
falls outside the final approach zone, `movement_worker.py` waits for that
specific animation to end before sending its first Left/Right. The stationary
recovery event has already prevented later attack beats. This avoids the game
swallowing that initial walk key while the hold manager continues to renew a
worker-side hold that never reached the game.

That gate only covers the first key-down of one return. A key-down swallowed
later in the same walk — by a hit reaction or by a cast — is caught by a progress
watch that belongs to the walk itself: while the 站桩 return walks toward its
temporary anchor, the worker tracks the marker's distance to that anchor, and
after `STATIONARY_RETURN_REARM_NO_PROGRESS_FRAMES` (5 captures, about one second
at the shared cadence) without a 0.001 X improvement it releases the walk hold and
arms the same direction again. The watch is reset by any progress, by a change of
direction, and whenever the decision is no longer a far stationary return, so an
ordinary patrol hold is never interrupted by it.

### Combat and consumables

`attack_worker.py` performs fixed-interval attack and optional jump attack. `drug_worker.py` prioritizes HP/MP thresholds and runs pet food as a non-movement timed consumable. Small-step and stair-jump logic are isolated from ordinary patrol decisions so recovery behavior does not repeatedly enqueue the same input.

In stationary attack mode, the temporary anchor is captured only on a manual patrol start. Its X/Y recovery band is local to that anchor and never depends on a recorded layer. A requested stationary facing is queued only after a real recovery or a later setting change: startup deliberately records the current position without injecting a directional tap. 双向 changes the desired facing after 80 settled observations; the arbiter performs one short facing tap rather than a walking movement.

That facing tap is a 30 ms turn, deliberately as short as the tiny anchor step: it exists to turn the character, and a longer hold walks it out of the anchor band, after which the correction walks it back, turns it again and re-arms the facing — the face/walk twitch.

The anchor correction is one atomic motion per correction, shaped as “direction hold, then the attack belonging to that correction”. The hold is the tiny step inside the small-step band (±0.010X of the anchor) and the longer walk when the character is further out; both carry exactly one attack. The design reason is direct field evidence: as an ordinary walk hold the correction either deferred the fixed cadence (direction handoff) or blocked it (exclusive recovery), so the character corrected its position and attacked nothing. Corrections are spaced about 300 ms apart and repeat until the marker is inside the arrival band (±0.006X). That band is not widened to “hold” a small drift: a position that is off the band is always walked back, because tolerating it left the character standing a pixel or two away from its 桩.

Stair jump observes sustained position stalls and submits one direction-preserving recovery jump. It uses a post-trigger frame cooldown and a movement-progress reset, preventing a single long stall from producing a burst of jumps. Recorded jump points are separate from this recovery rule: they are deliberate X/Y triggers, have their own once-per-leg pass record, and can fire while a rope climb is active.

**被撞反击** is an event path beside the fixed cadence, not a mode of it, and its two halves deliberately live in two workers:

- **`status_worker.py` owns the fact of a hit.** The HP reading is trusted only at action-grade confidence, and a hit is a *decrease* between two such samples, so a one-frame wobble in a weak OCR/colour read cannot press a direction key. The worker publishes `(previous_hp, current_hp, frame_sequence)` through a callback and does nothing else with it — it never chooses a direction and never sends input.
- **`movement_worker.py` owns the direction and input.** It records the exact hit-frame marker X in a twelve-sample window — now as `(sequence, x, planned patrol direction)` under `_counterattack_marker_lock`, because the status thread resolves the reaction from that window instead of only the movement thread — and claims the three-frame HP-drop cooldown when the hit is decided. `_queue_counterattack_from_samples()` is called immediately from `notify_hp_drop()`, so a hit whose marker sample is already published is queued without paying another capture tick; the sample may sit up to `COUNTERATTACK_MARKER_SEQUENCE_TOLERANCE` (2) captures from the HP frame, and only a genuinely unpublished sample leaves the hit on the retry queue. The evidence for the direction is the displacement across the hit frame, or, when that is below `COUNTERATTACK_X_DIRECTION_MIN_DELTA` (0.003), the largest motion over `COUNTERATTACK_IMPACT_LOOKBACK_FRAMES` (4) captures that opposed the planned walk. In stationary mode it places only the bound key into the shared one-use `AttackSlot` (`attack_slot.py`, `replace_next()`), so the next cadence, anchor-correction, or 小碎步 attack spends that key instead of the default one and no counterattack movement is created at all. On patrol, a clear displacement keeps the inferred direction; a compensated hit queues `request_counterattack(direction, key, hit_x)` so the later reaction can step back toward the saved X, and the token is sent with `direction="none"` when no direction survives — the key is then tapped without a step.
- **The wiring is coordinator-owned.** `assistant.py` creates the single `AttackSlot`, passes it to `AttackWorker` and to `MovementWorker.set_attack_slot()`, sets `status_worker.hp_drop_callback` and `motion_arbiter.set_counterattack_callback()` only after both workers exist, and `ui_worker.py` applies the checkbox state through `MovementWorker.set_counterattack()`. Status and movement therefore share the hit without importing or controlling each other (concurrency rule 5), and the one-use slot is the only piece of state the two attack paths may share.

### Alerts, disconnects, and reconnect

`character_worker.py` identifies marker loss, disconnect candidates, other-player conditions, and related alerts. A marker absence alone is not a disconnect:

1. the yellow marker must be absent for 50 shared frames;
2. the same frame must pass the login-page check supplied by `reconnect_worker.py`, or match its dedicated offline-prompt square;
3. only then may disconnect alerting, patrol stop, and reconnect begin.

If the absence threshold occurs without either offline evidence — for example in a minimap-less zone — the worker enters a quiet paused state. It resets and resumes normally when the marker returns. `reconnect_worker.py` owns the prompt dismissal, login-page interaction, world/channel selection, and confirmation clicks; its frame-only evidence checks are also used for the disconnect gate.

World selection is intentionally its own verified gesture. `reconnect_worker.py` sends a double-click to the configured world row, waits 0.5 seconds, checks the page again, and only sends the second double-click if the world page remains visible. A transition to the channel page after the first double-click ends the step immediately; the second gesture must never land on a channel row. This path has no single-click or Enter fallback, because either can confirm the wrong/default world.

The reconnect-only drop-recovery policy belongs to `movement_worker.py`, but is armed by the reconnect restart path in `assistant.py`. It remains inert for a manual patrol start and for a reconnect that begins within the route. After three genuine down-jump attempts without enough positive Y progress, it probes left for five seconds, then right for five seconds only if left did not reveal a drop. A successful descent returns control to normal drop-to-route movement.

`auto_restart_worker.py` is a distinct memory-leak recovery lifecycle. It samples Windows system memory every 30 seconds and publishes that reading to the authorization header whether or not restart is enabled. At or above 98% with 自动重开 selected, it stops patrol, sends the optional 重开消息, signs out, terminates the game process tree, and waits without a fixed abandonment timeout until the old game window and process tree are gone. It then focuses the persistent `launcher3.0` dialog and clicks its measured launch point. A newly visible game window is not enough to start reconnect: the worker re-focuses it once per second while waiting for `ReconnectWorker`'s existing login-page colour evidence. Only a confirmed login page receives the dedicated reconnect handoff. If the fresh game remains visible but login/reconnect cannot complete, it is retained rather than killed into a restart loop. Auto-restart reports its lifecycle through the 图层校准与逻辑 status line; the Additional Functions panel contains controls only.

### Other-player detection and the channel switch

`movement_worker.py` owns the 有人换线 workflow: the per-frame red-marker scan (`_maybe_check_other_players`), the staged request/wait phase, and the switch loop. `channel_routing.py` plans a route (pure functions), `channel_switch.py` sends it, and the coordinator is told only about a channel the character is really on.

Detection and proof are deliberately separate concerns:

- **Detection is a colour-family question with the marker's own shape floor.** `marker_detector.detect_red_diamonds()` accepts only strongly red pixels — `red ≥ 190`, green and blue below their ceilings, saturation ≥ 0.60 — whose red dominates green three to one and **blue eight to one** (`RED_MIN_RED_BLUE_RATIO`). The shape model is deliberately not part of the tightening: the operator's marker is a 2×2 block (a square, or a diamond when the client draws it rotated), so the gate stays "minimum 3 pixels, span ≥ 2, aspect 0.45–2.2, compactness ≥ 0.50" and only the colour range moved. The calibration is the operator's own picture of a map with nobody on it (`red_markder_missing.jpg`, a 150 %-scaled desktop capture): its reds are a magenta-red UI red of `(203, 0, 32)` and pinkish glyph reds of `(209, 29, 36)` and `(192, 5, 44)`, and the client's marker shades `(255, 0, 0)`, `(227, 0, 0)`, `(200, 0, 0)` and `(190, 20, 20)` all sit at 9.5× or more. Those false reds sit in marker-sized blobs (5×5 to 8×8 in that capture), so a size or aspect gate cannot separate them from a real marker without risking the marker itself; the shape model therefore stays untouched and the separation is made where the two populations are furthest apart — the red-to-blue ratio.
- **A channel change is proven by the map, not by a timer.** `_wait_for_switch_reload()` treats the marker's absence for `OTHER_PLAYER_SWITCH_RELOAD_FRAMES` captures (the client's loading screen hides the whole minimap) followed by its return as the only success test. The old `_wait_for_switch_evidence()` wrapped that same signature in a twelve-second budget and additionally required four clean or two occupied captures before it answered, so a verdict came from elapsed time rather than from the map: the field log of 2026-10-03 shows three attempts on one channel all reporting "no loading screen" at 13 s each while the UI had already been committed to the target. The only remaining bound is a hang guard counted in fresh captures (`OTHER_PLAYER_SWITCH_RELOAD_IDLE_FRAMES`): a marker that never goes away means the change never started, and the same target is re-planned at once instead of after a spent budget.
- **Committing the channel and asking whether the new channel is occupied are separate steps.** `_note_channel_landed()` commits to `user_config.json`, to the reconnect worker and to the 频道 field through the coordinator's `note_player_channel_landed()` immediately after the reload is proven and before the occupancy question, so the field follows a successful change whether or not somebody is on the new channel, and a route that never took the character off its channel never commits at all. `_wait_for_new_channel_occupancy()` then answers exactly one thing — switch on to the next channel of the route (two confirmed captures of a red marker) or resume patrol (four consecutive clean captures) — with the post-reload settle (`other_player_switch_settle_seconds`) giving the new map time to draw its markers.
- **Ownership and exit.** The workflow runs on its own thread, stands the patrol down through the patrol controller, and injects keys only through the shared sender, so no stale held direction keeps the character walking while the menu keys are sent. A fresh patrol start cancels it (`reset_other_player_switch`) and Esc cancels it through `WorkflowCancelWorker`. A workflow that ends without a proven change leaves the patrol stopped; only a cleared mission (an empty channel reached, or the other player gone) hands it back. A failed attempt is re-planned from the channel the character is really on, so the same target is retried `OTHER_PLAYER_SWITCH_SAME_TARGET_ATTEMPTS` times before the route moves on.
- **Timing is one presence window plus a configured wait, both in seconds.** `OTHER_PLAYER_INITIAL_PAUSE_SECONDS` (180 s) is the whole presence phase: the 求让消息 goes out at a random `OTHER_PLAYER_REQUEST_MIN_SECONDS`–`OTHER_PLAYER_REQUEST_MAX_SECONDS` (30–60 s) point inside it, and the workflow then watches only to the three-minute boundary, so a departure at any time ends it. The 多等 delay is the operator's own value in **real seconds** (`player_channel_wait_seconds`, 0–1200, default 300) — the unit that makes the 组队 hand-over work — and `OTHER_PLAYER_TIME_SCALE` (1.0 shipped) multiplies every duration in the workflow so a shortened field test changes one constant instead of the timing code. The UI stores seconds and migrates a pre-change `player_channel_wait_minutes` value by ×60 on load, so an old five-minute preference cannot silently become a five-second switch.
- **A resumed patrol is prepared, never merely switched on.** After a proven change onto an empty channel the workflow asks the coordinator (`on_other_player_resume` → `prepare_patrol_after_other_player_channel()`) to re-run the reconnect map/layer preparation before `patrol_controller.set_enabled(True)`. A channel can spawn the character above the recorded range, and that preparation is what arms the existing above-route `drop-to-route` path from a fresh marker reading. If it cannot be completed, the patrol stays stopped with a warning instead of walking on stale route state; a cleared mission (the other player left, no switch needed) needs no preparation and hands patrol back directly.

### Automatic lie handling

`api_lie_test.py` is the local client adapter. `autolie_api/` is vendor reference material and must remain unchanged.

At UI/application startup, `assistant.py` performs an inexpensive WebSocket endpoint probe and caches the usable endpoint. When a lie event occurs, the adapter uses that cached endpoint to create a fresh authenticated connection while the game completes its visual transition. The event timeline is:

```text
event start ── connect/setup in parallel ── 3 s visual settle
            └──────────────────────────────► RTF1 frame uploads, at most 15 s
                                                └► round_end + WebSocket close
```

`rtf1_burst.py` owns the application-level RTF1 flow-control window. At frame
standard 5 it permits three outstanding frame IDs; the automatic pass polls
available JSON `frame_result` messages before sending the next capture. It
does not create concurrent WebSocket writers: the pass worker is the only
socket owner, while capture, cursor execution, and UI remain separate.

A full window is treated as back-pressure rather than loss. `api_lie_test.py`
checks `has_capacity` before capturing, so it neither encodes a JPEG it must
reject nor advances `frame_id` past a frame the server never saw (IDs must stay
contiguous); while it waits it reports `pending_count/max_in_flight` at most once
per second, and a stall of `FRAME_RESULT_STALL_SECONDS` (2.5 s) raises a
`ConnectionError` to end the pass before the service's own ten-second no-frame
timeout closes the socket. That error path sets no `completed_successfully`, which
is exactly what the UI layer gates on: `UiWorker` schedules the dialog's measured
confirmation click and the patrol resume only for a pass that completed its
frame feed, and leaves a transport-failed pass to the operator
(`自动过测谎: pass ended before completion; confirmation click and patrol resume are
skipped`).

The **测试API** video drill uses the same packed-JPEG handshake and sends no
frames during its three-second visual settle. Its connection log and summary
record the transport and handshake duration for field verification.

The live frame stream uses the shared capture cadence. The temporary detection rectangle and one-shot target marker are display-only overlays and must never become tracking input.

A server answer is fed to the real cursor as a physical desktop point. `api_lie_test.py` converts the answer with `point.to_screen()`, the same conversion its overlay draws with, and pushes it through `MouseAimController.push_screen_target(..., immediate=True)`. Sending it as client pixels would re-map an already-correct screen coordinate through the video's client rectangle, which is wrong under DPI virtualization because the captured bitmap and Windows' logical client size differ; `immediate=True` also bypasses the interactive viewer's dead band and speed limit, both of which are hover affordances rather than part of an automated pass.

When the pass ends, `ui_worker.py` focuses the game and uses `click_screen()` to press the lie dialog's measured confirmation point. The point is `(630, 428)` on a 1080×768 client; all other clients derive from `(800, 472)` in the 1366×768 reference layout by width scaling. This post-pass acknowledgement is separate from WebSocket completion and must complete (or safely refuse when focus is unavailable) before patrol is resumed.

### UI, configuration, and optional tools

`ui_worker.py` presents the Chinese desktop interface. It reads and writes only through configuration callbacks supplied by the coordinator. UI redraw work is deferred during resize/drag operations to avoid black component flashes and expensive intermediate layouts. `screen_blinker.py` owns click-through diagnostic overlays: crosshairs and all patrol-point symbols are painted on persistent native canvases, never into capture input.

`HoverTooltip` is the shared, cursor-adjacent hover-message component. The
alert selections use it to explain their visible effects, and the automatic-lie
selection additionally explains its mouse ownership, Esc interruption, and
accounting behavior. Tooltips have a shared maximum width and wrap only at that
limit.

Telegram setup is also UI-owned but delivery is isolated in
`telegram_notifier.py`. A normal left click on **设备名称** opens a set/clear
dialog; setting a bot token updates the button to **已设置**. The notifier uses
the Windows system HTTP proxy first, then probes common local HTTP proxy ports,
then attempts a direct connection. Its selected proxy is cached, invalidated,
and detected again after a transport failure, so proxy configuration never
blocks the UI or gameplay workers. The title-bar teaching viewer independently
loads one JPG at a time from packaged `teach-assets/`; `build_release.ps1`
copies that folder unchanged for both package formats.

A diagnostic region is converted from captured-pixel coordinates to screen coordinates by `_capture_pixel_to_screen()`: scaling through the logical client rectangle is correct only while the captured bitmap matches it, and when the bitmap is DPI-virtualized (a different width/height) its pixels are already screen pixels relative to the capture origin. That conversion is display-only. The logical window rectangle that game input and the auto-lie cursor workflow use is never derived from it — changing the shared rectangle to fix an overlay was what made earlier builds unstable.

Each recorded layer row carries an axis band whose point menu (添加最左 / 添加绳索 / 添加最右 / 添加左跳 / 添加右跳) opens only on a **right click**. Left-clicking never opens the menu; it remains reserved for normal selection behavior. The menu itself is refused while patrol runs, because recording is locked then.

One optional control deliberately uses its own unit: the 捡东西 (stand-still pickup circuit) trigger interval is set in **minutes** (`每 … m`, 2.0m to 30.0m). The row converts before it publishes and after it loads, so `stationary_pickup_interval_seconds` and the movement worker’s own bounds stay in seconds; the interval is clamped to the same two minutes at the bottom and thirty at the top in both places, so a hand-edited configuration cannot schedule a sweep every few seconds. The random gap in that row remains a seconds control.

The other-player 多等 delay is the mirror image of that rule: it is a **seconds** control (its validator accepts 0–1200 and `_normalized_player_channel_wait_seconds()` clamps to the same range), published as `player_channel_wait_seconds` and consumed by the movement worker in real seconds. A configuration written before the seconds UI stored whole minutes, so loading migrates it by ×60 rather than reading the old number as seconds — a stored five meant five minutes, and reading it as five seconds would have turned the operator's hand-over delay into a no-op. The field commits on blur and on an outside click, because clicking a label or a panel does not move Tk focus, and its hint text is rebuilt from the displayed value so the row never explains a different number than it shows.

Combat panel state is applied in both directions. The 被撞反击 checkbox and its 攻击键 button live on the 巡逻攻击 row and publish `counterattack_enabled` / `counterattack_key` into `fixed_attack_settings.json`; the mode handler pushes them through `MovementWorker.set_counterattack()` for 巡逻攻击 and 站桩攻击 and clears them when no attack mode is active. The stationary-only lock is re-applied after a settings load and after an authorization unlock, because the generic unlock enables every product widget and previously left a saved 巡逻攻击 session exposing 跳打/朝向 controls until the operator switched modes once.

Personal settings are stored in `user_config.json`; application defaults are stored in `system_config.json`. Runtime scratch data belongs under `work/`, including patrol state and timer persistence. Critical exceptions additionally go to `error.log`.

`hotkey_worker.py`, quick messages, trade helpers, Telegram notification, screen blinking, countdowns, and shutdown logic are optional subsystems. They use isolated configuration/state and may request input only through the shared focus/key boundary.

## Data and persistence

| Location | Owner | Contents |
| --- | --- | --- |
| `user_config.json` | UI/config callbacks | User choices, keys, patrol layers, message list, alerts. |
| `system_config.json` | Application defaults | Internal defaults and non-personal settings. |
| `hotkey.json` | Hotkey worker | Ordered hotkey bindings. |
| `fixed_attack_settings.json` | UI / fixed-attack panel | Interval, attack key, combo slots, 跳打/小碎步/朝向, 被撞反击 enable + key. |
| `work/` | Runtime workers | Recoverable patrol/timer/session state. |
| `lie_accounting_pending.json` | `lie_accounting.py` | Durable auto-lie event outbox: one row per unresolved `started`/terminal report. |
| `error.log` | Error reporting | Critical unexpected-error record. |
| `server_client.log` | Licensing transport | Safe activation connection, pin, HTTP, and local verification events; never secrets. |
| `assistant-launch-error.log` | `startup_probe.py` | Hidden-launch failures, including the missing dependency that triggered the automatic install. |
| `assistant-launch-status.log` | `startup_probe.py` | Launcher milestones: probe reached, repair started/finished, normal exit. |
| `teach-assets/` | UI teaching viewer | Shipped JPG pages, loaded one at a time in natural filename order; copied unchanged into both packages. |
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
