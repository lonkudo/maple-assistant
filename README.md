# TodoHelper

TodoHelper is a Windows desktop companion for recorded minimap patrols. It combines a compact Chinese interface, shared screen capture, map-aware movement, configurable attacks and consumables, alerts, recovery tools, and optional quick-message/trade helpers.

The assistant is designed around one principle: **automation must be observable, interruptible, and safe to stop**. Patrol input is disarmed until a patrol is explicitly started, and every worker uses the same foreground-window safeguards.

## Install and start

Two packages are published for each version:

| Package | Audience | Start |
| --- | --- | --- |
| `TodoHelper-<version>.zip` | normal package, needs a Python environment | extract, run `安装.bat` once, then `启动助手.bat` |
| `TodoHelper-release-<version>.zip` | standalone package, no Python installation | extract, run `启动助手.bat` |

Normal package:

1. Extract the newest `TodoHelper-<version>.zip` to a normal writable folder.
2. Run `安装.bat` once. Approve the single Windows permission prompt when requested.
3. Run `启动助手.bat`.

TodoHelper has one product edition. Licenses no longer select NORMAL or NP:
the server issues one unified license type and the desktop does not gate
features by an edition label. Legacy signed NORMAL/NP documents remain valid
only for compatibility.

Standalone package:

1. Extract the whole `TodoHelper-release-<version>.zip` folder — the executable needs the files beside it, so running `TodoHelper.exe` alone is not supported.
2. Run `启动助手.bat` and approve the Windows permission prompt. The launcher is hidden and requests administrator rights, because the assistant must run at the same privilege level as the game or Windows silently drops injected keys.
3. If the assistant does not appear, run `诊断启动.bat` (`diagnose_start.bat`); it keeps its console window open and prints the startup error.

The standalone package starts no console window and needs no `安装.bat`: it carries its own Python runtime, the complete data layout of the normal package (configurations, assets, model weights), and the same `user_config.json` behavior. It ships no application source, so features that must start a *separate* Python interpreter cannot work there — the YOLO detector panel is the known case, and it stays hidden.

If a required library is missing, the normal package repairs itself: the hidden launcher writes the traceback to `assistant-launch-error.log`, runs `安装.bat` once automatically, and starts the assistant again in a fresh process. A failed automatic install leaves the log and a message box instead of a silent start.

The update icon in the title bar searches the Desktop, the running folder, and its parent folder for a newer compatible release ZIP. A fixed packaged helper (`todohelper_update.ps1`) waits for the current process to exit, then installs the ZIP, preserves `user_config.json`, removes the consumed ZIP, and restarts the app. It is the same reusable process for normal and standalone releases; no update-specific script is generated. Normal and EXE packages only replace their own format. **导入配置** is deliberately separate from package updates: it validates the selected JSON, stops patrol, atomically replaces the live configuration in a background task, then redraws the user-owned panels in the current assistant. The status line shows `↻ 正在刷新导入配置…` while this happens; it does not need a manual restart.

### Current release behavior (v1.2.2)

- The application is branded **TodoHelper**: its visible title, standalone executable, launchers, installer, and release ZIPs use that name. The configured game-window title remains independent and is unchanged.
- The first TodoHelper EXE release must be installed by extracting it normally. Once installed, later TodoHelper EXE ZIPs can be applied with the in-app update icon through the same persistent helper.
- A manual start of **站桩攻击** is always a fresh session. It records a new temporary anchor and clears the previous runtime pickup, route-return, climb, and pending-route state. Automatic reconnect/auto-lie resumes deliberately retain the current temporary anchor instead.

### Current release behavior (v1.2.20 – v1.2.21)

- **Layer identity for a one-supporter layer.** A layer may record a single supporter; its one-sided band is now `[y, y + small_gap]` with `small_gap` one marker row instead of the fixed 0.002 (0.28 px), so the floor the character is walking can be recognised. See *Layer bands and one-supporter layers*.
- **Recorded jump points fire once per leg**, not once per entry: a landing, a walk back over the same spot, or a climb past the same row no longer re-arms the point, and a climb may use one at most once per pass. See *Directional jump points*.
- **A return climb confirms the floor it actually reaches**, including a recorded floor outside the patrol range, and tolerates the character's own jump arc just above the platform instead of restarting the rope grab forever. See *Returning to the route from a lower floor*.
- **A floor that never recorded a rope no longer borrows the legacy profile-wide `rope.x`**: its own recorded jump point is used as the rope approach, and when it has neither, the return stands still and logs what is missing instead of walking off the platform.

### Current release behavior (v1.2.52 – v1.2.54)

- **Reconnect drop recovery is reconnect-only.** If an automatic reconnect starts above the recorded route and three ordinary drop attempts produce no downward Y progress, it tries a five-second left edge walk, then (only if that also fails) a five-second right edge walk. Any confirmed downward movement immediately returns it to the ordinary drop-to-route flow. A manual start and a reconnect that does not need to drop never enter this fallback.
- **World selection is a guarded double-click sequence.** The selected world row (for example 蘑菇仔) receives a double-click, a 0.5-second wait, then a second double-click only if the world page is still visible. If the first double-click has already opened the channel page, the second gesture is skipped so it cannot click a channel accidentally. The obsolete single-click and Enter confirmation fallback are not used for this step.
- **After automatic lie handling, the assistant clicks the measured confirmation point** rather than sending Enter. The 1080×768 point is `(630, 428)`; other layouts use the 1366×768 reference point `(800, 472)` scaled by client width. The game is focused before this click, and patrol resumes only after the click attempt completes.

### Current release behavior (v1.2.78 – v1.2.88)

- **A return drop recognises the patrol floor it landed on even when the marker reads outside that floor's recorded band.** The drop used to end only on a strict band match over the patrol floors, so a character standing on the patrol floor whose recorded points sit elsewhere on the same platform kept sending Alt+Down. The landing now also accepts the at/below-bottom-floor and bounded nearest-floor answers ordinary patrol uses, once an Alt+Down chord has really moved the marker down and the reading has held for two settled frames. The world-Y tracker is never used for the landing. See *Dropping back into the route from a higher floor*.
- **The map canvas (the yellow marker/patrol rectangle) is measured from the captured minimap, never estimated from the minimap window.** The old fallback derived the canvas from the outer frame's proportions (a fixed header height and bottom offset), and when the inner border was unreadable it widened the region back to the whole minimap window. It is now the border rectangle nested directly inside the window's border, measured per frame; an unreadable frame reuses the canvas measured earlier in the session, and when none exists the yellow rectangle is not drawn at all instead of covering the window. See *The map canvas and the detection overlay*.

### Current release behavior (v1.2.104)

This release carries the changes below: the channel-change proof, the 频道 field
that follows it, the narrower red-marker colour family, the hit reaction
**被撞反击**, 多等 entered in seconds, and the map re-anchor that precedes a
resumed patrol.

- **A channel change is proven by the map reloading, not by a time budget.** The
  yellow marker must go away (`OTHER_PLAYER_SWITCH_RELOAD_FRAMES` captures — the
  client's loading screen hides the whole minimap) and then come back on the new
  channel; that disappearance and return *is* the success test. The old
  twelve-second evidence budget, which also required four clean or two occupied
  captures before it would answer, is gone: the field log of 2026-10-03 shows
  three attempts on channel 41 all answering *no loading screen* and spending
  13 s per attempt on a verdict that came from a timer instead of from the map.
  A marker that stays visible for `OTHER_PLAYER_SWITCH_RELOAD_IDLE_FRAMES`
  (about five seconds of captures) means the change never started, so the same
  target is planned again immediately.
- **The 频道 field follows a *proven* change.** The channel is committed once the
  map has reloaded onto it — not when the menu keys were merely sent, which is how
  the field showed `41` while the character had not been confirmed to leave `29`.
  It is committed whether or not the new channel has another player; somebody
  being there only decides the next action (switch on to the next channel of the
  route, or resume patrol on an empty one).
- **The other player's red marker is recognised by colour, not by size or shape.**
  The marker keeps its own shape rule (the client's 2×2 block, a diamond when the
  block is drawn rotated), and the colour family now requires red to dominate
  *blue* eight to one (`RED_MIN_RED_BLUE_RATIO`) instead of three to one. The
  operator's own picture of a map with nobody on it (`red_markder_missing.jpg`, a
  150%-scaled desktop capture) measures the reds that used to pass: a magenta-red
  UI red of `(203, 0, 32)`, where red is only about 6× its blue, and pinkish glyph
  reds of `(209, 29, 36)` and `(192, 5, 44)` (about 5× and 4×). Every shade the
  client draws the marker in — `(255,0,0)`, `(227,0,0)`, `(200,0,0)`,
  `(190,20,20)` — sits at 9.5× or more, so the narrower ratio keeps the marker
  while the pink and magenta reds that used to register as another player do not.
  See *Switching channel when another player is present*.
- **被撞反击 answers a confirmed hit with its own attack key.** The HP bar, not
  a timer, is the trigger. Stationary mode replaces the next approved normal,
  tiny-step, or return-walk attack slot without adding a second movement. On
  patrol, a clear knockback keeps the short reaction step; when normal walking
  masks the displacement (< 0.003 X), the hit-frame X is retained and the short
  step returns toward that saved position before tapping the counterattack key.
  The three-frame cooldown prevents one damage animation from stacking reactions.
  See *被撞反击 (hit reaction)*.
- **多等 is entered in seconds.** The other-player channel switch waits the
  configured number of **seconds** (0–1200) after a proven need to leave, which is
  what makes the last-minute 组队 hand-over possible; the old field stored whole
  minutes. A configuration written before this change is migrated on load
  (`player_channel_wait_minutes` × 60), so an existing five-minute preference does
  not silently become a five-second one. See *Switching channel when another player
  is present*.
- **A resumed patrol starts from a freshly anchored map session.** After a proven
  channel change onto an empty channel, the coordinator re-runs the same
  map/layer preparation the reconnect path uses before the patrol is handed back,
  so a character that spawns above the recorded range drops back into the route
  first instead of walking horizontally with stale layer state. If that
  preparation cannot be completed, the patrol stays stopped and the log says so.
  See *Switching channel when another player is present*.

### Current release behavior (v1.2.105 – v1.2.112)

These releases harden the two event paths that must never steal a beat from the
operator: the 被撞反击 reaction and the automatic lie pass.

- **被撞反击 replaces an approved attack beat instead of adding a movement.** A
  shared, one-use `AttackSlot` (`attack_slot.py`) is created by the coordinator and
  held by both the cadence worker and the movement worker. In 站桩攻击 a confirmed
  hit only puts its bound key into that slot; the next attack beat that was going
  to happen anyway — the fixed cadence, the anchor correction that carries the
  stationary return, or 小碎步's middle attack — spends it instead of the default
  key. The character therefore never receives a second movement transaction for
  one hit, which is what used to fight the anchor correction. An unconsumed hit is
  retained (a second hit does not overwrite the first) and is consumed exactly
  once.
- **The cooldown belongs to the HP drop, not to the arbiter's execution.** The
  three-frame window (`COUNTERATTACK_COOLDOWN_FRAMES`) starts when the HP fall is
  decided, so a damage animation that lowers HP across adjacent reads produces one
  reaction rather than a row of delayed ones.
- **A hit is answered on the spot, from published marker history.** The HP bar and
  the minimap are analysed on independent threads, so the reaction is resolved from
  the marker samples the movement worker has already published: the matching
  capture sequence, or — when the two threads landed a fraction of a capture apart
  — the nearest sample within `COUNTERATTACK_MARKER_SEQUENCE_TOLERANCE` (2
  captures), logged as `被撞反击: HP frame … used nearby minimap frame …`.
- **A knockback hidden by ordinary walking is still answered.** When the marker
  moved less than 0.003 in X at the hit frame, the lookback of four captures
  (`COUNTERATTACK_IMPACT_LOOKBACK_FRAMES`) is searched for motion *against* the
  planned patrol direction — the push the character was walking into. When that is
  found, the reaction steps the other way; when it is not, the reaction still
  happens, carrying the hit-frame X so `perform_counterattack()` can step back
  toward the saved position before tapping its key, or tap the key alone
  (`direction="none"`) when the marker has not moved a whole minimap column.
- **A far stationary return re-arms its direction after about 0.6 seconds without
  progress.** A 站桩 return that walks a long way back to its 桩 sends an ordinary
  held direction; when a hit reaction or a cast makes the game ignore that
  key-down, the hold used to be renewed invisibly while stationary recovery
  suppressed every later attack. After
  `STATIONARY_RETURN_REARM_NO_PROGRESS_FRAMES` (3 captures, about 0.6 seconds) with
  the marker not closing on the anchor, the hold is released and the direction is
  armed again.
- **The RTF1 pass waits for the server instead of discarding frames.** A full
  negotiated window is brief back-pressure, not a failure: the pass stops capturing
  while the window drains, keeps the next `frame_id` intact (frame IDs must stay
  contiguous), and reports `RTF1 队列已满，等待后端返回（n/max）` once per second.
  Silence longer than `FRAME_RESULT_STALL_SECONDS` (2.5 s) aborts the pass before
  the service's own ten-second no-frame timeout can close the socket underneath
  it.
- **A server-close is not a visual verdict.** Once feeding starts, the UI watches
  from 10 through 40 seconds for two consecutive 40×40 blue success-box matches
  or the black-room missing-marker failure. A closed socket leaves that observer
  running. If it is still visually unresolved at 40 seconds, the pass is recorded
  as success without clicking a fixed confirmation point; patrol resumes and the
  old detector square is ignored until it clears, after which the next real lie
  window can arm normally.
- **The drill moves the cursor to the same physical point as its overlay.** The
  answer point is pushed in desktop pixels
  (`MouseAimController.push_screen_target`) with `immediate=True`, so it is neither
  re-mapped through the client rectangle — DPI virtualization can make a captured
  bitmap and Windows' logical client size differ — nor filtered by the viewer's
  dead band and speed limit.
- **A finite auto-lie balance always beats the unlimited marker.** The desktop
  state treats a numeric `remaining_auto_lie_count` as authoritative, clears a
  finite balance only when the server sends null for it, and trusts
  `remaining_auto_lie_unlimited` only together with that null. A just-activated
  limited grant therefore shows its number immediately instead of 无限 until the
  next restart, and the usage line prints the count before the marker.
- **设备未就绪 is a local device state, not a server answer.** The authorization
  line no longer prints 设备：等待服务器返回 beside 设备未就绪，稍后重试, and clicking the
  设备码 shows （已复制） for 1.5 s before re-rendering from the authoritative status, so
  a heartbeat that arrives inside that second is not replaced by stale caption
  text.

### Current release behavior (v1.2.160 – v1.2.163)

- **A focus loss pauses the patrol; it does not end it.** `focus_worker.py` releases
  the held direction keys and clears the automation gate the moment the game stops
  being foreground, and re-opens it by itself when the game is focused again —
  whether the gap lasted a tenth of a second or an hour. Only a manual **停止巡逻**
  ends a run. The pause is reported once per episode (`AUTOMATION PAUSED`,
  `AUTOMATION RESUMED`); the previous "terminal stop after the grace window" no
  longer exists, and neither does the `on_focus_lost` callback that implemented it.
- **An unresponsive game is not a focus loss.** When the client hangs, Windows hands
  the foreground to a DWM *ghost* stand-in (class `Ghost`, owned by `dwm.exe`, title
  = the game's title plus a localized "not responding" suffix). A lag spike therefore
  used to satisfy "the game is not foreground" and stopped the patrol after 2.5 s.
  The foreground predicate now accepts that stand-in when its title is this window's
  own title plus that suffix, and it never excuses an unrelated window: a real
  focus thief, another window's ghost, or a vanished game window still count as a
  loss. A hang is reported once (`foreground: the game window is not responding …`).

### Current release behavior (v1.2.290 – v1.2.291)

- **The installer never asks for a code twice.** The launch window has no
  activation field. An existing installation's code comes from its own profile:
  the selected root's `助手配置` first, then the older build's
  `<selected>\app\助手配置`, and that document is silently moved to the selected
  root (`installer: moved the activation profile … -> …`) so the installed app
  keeps its activation. The code is taken from `license_settings.json` when the
  app stored the typed code, otherwise from the authenticated document's
  `_local.activation_code` (the `MAL-…` server code); only a folder with no
  profile opens the **首次安装需要授权码** window. Deriving a token from the
  signed document was tried and reverted - the server answers **设备未授权** for
  it.
- **Each layer starts on its nearest leg.** On entry the route takes the closer
  of the two recorded edges (a leg already reached is consumed at once), walks
  the other edge, then the layer's way-up phase: its recorded rope, or the up
  jump point when no rope was recorded. The order survives a split-part index
  change; a layer with neither rope nor up point keeps repeating its own legs.
- **The up jump point is a jump, not a rope.** It owns an `up` phase that only
  walks to the point (`near_target=False`, no Up hold, no climb) and fires only
  in that phase or through the dwell rule - never from the left/right or rope
  legs. A completed jump accepts the next floor immediately and the old floor's
  route waits `JUMP_UP_LANDING_WAIT_SECONDS` for the layer change instead of
  running its legs on the new floor.
- **The dwell trigger is reusable and bounded.** A point the character stays
  inside fires after more than three captured frames, again while standing (the
  dwell counter restarts on fire and the 0.4 s refire cooldown still applies),
  up to `JUMP_POINT_DWELL_MAX_ATTEMPTS = 3` per leg; the entry trigger keeps its
  once-per-leg rule.
- **A stalled character is jumped off, not dropped.** The self-rescue presses
  Left and Right for two captured frames each; no X movement on either side is
  a rope stall, so it jumps toward the CLOSEST recorded target (left-most,
  right-most, or rope of the patrolled layers) and then re-arms patrol from the
  nearest recorded floor. A refused key cancels the probe instead of confirming
  a stall, and a confirmed stall is never answered with Alt+Down - the
  unresolved-floor recovery probes the walk before its first chord for the same
  reason.
- **The next floor is recognised.** The flicker guard accepts a floor whose
  arrival band contains the marker, a blank detection frame no longer resets the
  layer-resync counter, and a completed up jump arms the resync.
- **World-Y bands are narrowed to the floor gap.** A saved band may no longer
  contain another floor's anchor: each band is clamped around its own anchor to
  just under half the distance to the nearest other floor, so a stair-height
  difference stays two bands and a reading between them matches none.
- **A horizontal leg keeps its recorded edge.** An out-of-span up jump point no
  longer stretches the left/right leg, so the walk cannot jump away before the
  other leg runs.
- **The launcher shows no console.** `start_assistant.bat` and `启动助手.bat`
  start `wscript` detached, so the cmd window closes before the UAC prompt
  instead of hanging around the confirmation.

### Current release behavior (v1.2.278 – v1.2.280)

- **An unresolved floor changes layer instead of walking it.** When the marker
  matches no recorded band and is farther than the nearest-floor allowance from
  every recorded row for four settled frames, TodoHelper presses Alt+Down. After
  two chords that produce no downward progress it walks toward the nearer outer
  side (X > 0.5 → left, X < 0.5 → right) for 5 s to find a drop edge, then tries
  the opposite side; a descent during either walk resumes Alt+Down immediately.
  The recovery is bounded (3 edge cycles or 12 chords) and yields to every other
  vertical owner — a return, the planned descent to the first layer, an attached
  climb, a jump-point Up claim, the reconnect drop recovery, a pickup leg, or a
  pending reconnect route check (`UNRESOLVED FLOOR: layer change deferred while …`
  names the owner). It resets on Start Patrol and whenever the marker resolves.
- **The landing floor is recognised over every recorded floor.** The bounded
  nearest-floor answer now considers all recorded floors, not only the patrol
  range: with a single-floor range (`patrol_start_layer = patrol_end_layer`) the
  floor a descent is heading to was not even a candidate, so a marker standing
  0.0104 from layer1 was refused as `0.1248 away from layer2`. The per-frame
  resolver's fallback uses the same set.
- **The world-Y origin is only taken from the marker.** 自动重连 respawns the
  character in mid-air, so the scroll-compensated world Y is untrusted
  (`WORLD-Y: the scroll-compensated value is not trustworthy (…)`) until the
  marker itself confirms a standing floor. The post-reconnect route check
  re-anchors only on marker/bottom evidence, and a world-only answer may decide
  the route but never re-anchor itself.
- **A jump-point Up claim can no longer outlive its jump.** A claim that produces
  no settled landing, no rope handoff and no rising marker is released after 3 s
  (`JUMP POINT: released a N.Ns Up claim that never produced a landing`); every
  forced key release drops the claim together with the key, a new Start Patrol
  cannot inherit it, and the stale-session handoff after a fall accepts two
  downward frames when the settled reading is strictly inside the lower floor's
  band.
- **No patrol-start grace on rope jumps.** The three-second "do not jump onto the
  rope the character happens to start next to" hold is gone: standing inside the
  rope band means that jump is the wanted action, and the hold cost three seconds
  of every Start Patrol at a rope while attack stayed blocked. The stair-jump
  stall detector keeps its own start grace.
- **The reconnect drop recovery walks an edge after two stalled chords** (was
  three) and yields to the unresolved-floor recovery unless a `drop-to-route`
  return is actually running.
- **Five timed buffs, and a stated post-tap window.** A buff tap holds the key
  200 ms, then reserves a 1.0 s quiet window
  (`motion arbiter executed buff:<key> (lock 1.00s)`) during which no other motion
  and no attack can run; the timer restarts only after it. The cast check compares
  HP/MP 2.5 s after the tap, so a buff that moves neither bar always reports
  `no HP/MP change within 2.5s after the tap`.
- **The movement log states why it is idle.** A held frame prints its own reason
  (`action=wait (unresolved floor; waiting for the Alt+Down layer change)`), and a
  rope-zone target prints as `target=0.188596*` instead of `----`.

### Current release behavior (v1.2.252)

- **Customers can securely解绑 a counting-mode authorization from their own
  device.** The authorization line now has **解绑** after **备注**. It asks for
  confirmation, sends the locally stored original authorization code and the
  local fingerprint only through the certificate-pinned activation service,
  then shows a Chinese success or failure dialog. On success the client copies
  the original code, deletes its local signed entitlement, clears the in-memory
  auto-lie credential, and immediately locks all automation on that computer.
  The code remains usable for activation on the replacement device.
- **Unlimited-mode codes cannot be self-unbound.** The server returns
  `测谎未结算，请联系管理员结算后解绑。` even when the unsettled count is zero;
  settlement is an operator-controlled boundary. The server allows at most two
  successful customer unbinds per equipment per GMT+8 calendar day. A rejected
  request never copies the code or locks the current desktop.
- **Automatic-lie capacity selection is fast and distributed.**
  `auto_lie_backend.py` holds fourteen `host:port` WebSocket instances:
  `221.10.121.69` ports 48001–48003 and 48007/48009/48011/48013/48015, plus
  `117.50.223.113` ports 8001–8003 and 8007/8009/8011. The startup preflight
  shuffles the full list, probes a concurrent random group of three, and accepts
  the *first responding* instance with `slots_left > 0`; it does not wait for
  slower sibling probes. The winning probe is only a capacity observation, not a
  reservation, so the live pass always creates a fresh authenticated connection
  and may fail over through the rest of the same shuffled order if its handshake
  fails. The old `fingerprint % 3` assignment is gone, so a device is not pinned
  to one worker or host.
- **All automatic-lie logging lives in `auto_lie.log`.** The credential lines, the
  durable event-accounting lines, and the connection trace of the accounting
  request used to be written to the licensing connection log
  (`server_client.log`); `server_client.log` now carries licensing only, and the
  lie-events request's connect/pin/completed lines follow the feature into
  `auto_lie.log` (`pinned_tls.post_json(..., logger=…)`).
- **The credential can never appear in a log file.** A `CredentialRedactionFilter`
  is attached to every file handler (assistant trace, licensing connection,
  auto-lie, and the in-app running log). It rewrites the live credential *and* the
  vendor's masked preview of it (`mask_key`, which shows the key's first six and
  last four characters) to `(credential hidden)`, in the formatted message and in
  any argument.
- **The credential is fetched by the heartbeat and then kept.** It is not dropped
  before a heartbeat request, so a licensed device with passes left always holds
  its credential for the whole round trip. It is cleared on a failed heartbeat
  (transport, TLS, or device-not-ready), on a verdict that the license is expired
  or revoked, and when the server withholds it (an empty `auto_lie.secret_key`,
  which is what a device with no passes left receives). A **successful** heartbeat
  never clears it, and a reply that does not mention `auto_lie` at all leaves the
  held credential alone — silence is not a verdict. Whenever a heartbeat clears
  it, **自动过测谎** is unselected and greyed until a later heartbeat reports
  otherwise.
- **No lie window asks for a heartbeat.** The heartbeat is sent at application
  startup, on its own schedule (three hours after an accepted one, and at the
  GMT+8 midnight roll-over), after a failure, and when the operator presses the
  caption **↻** refresh (which does a heartbeat as well as refreshing the
  remaining-time line). A window that arrives before the credential does is
  reported nowhere and simply answered by hand — no per-window request, no log
  hint, no extra licensing round trips.
- **The 测试API button is hidden again.** The video drill itself is untouched in
  `api_lie_video.py`; uncommenting three statements in `ui_worker.py` puts the
  button back.
- **站桩攻击 correction tuning.** The arbiter step lock is **0.60 s** and is shared
  with 被撞反击, so it — not the 0.30 s re-request interval — is the effective
  spacing between repeated corrections; the near correction hold is **0.11 s**; the
  final approach band is **±0.030 X**. The pre-emptive wait for an in-flight attack
  before the first return key after a knockback is **gone**: a swallowed key is
  caught by the three-capture progress watch instead, so the character starts
  walking back immediately.

## Configuration

Two configuration files keep personal settings separate from shipped defaults:

| File | Purpose |
| --- | --- |
| `user_config.json` | Your keys, layers, patrol route, alerts, quick messages, and personal settings. |
| `system_config.json` | Application defaults and internal behavior settings. |

Use **导入配置** and **导出配置** in the running-log panel to move your personal configuration between installations. Import validates the selected JSON, stops patrol, replaces `user_config.json` atomically, and refreshes the current assistant without restarting it. Export writes `桌面\\助手配置\\新配置.json`. Import and export actions are recorded in the log. If **重置录制** cannot replace an inaccessible configuration carried over from an older release, the UI shows `配置版本与当前助手版本不匹配，请重新录制以后导出新配置。` rather than exposing a raw Windows access-denied error.

## Licensing and activation service

The desktop package verifies signed licenses locally using only an Ed25519
public key. The separate `maple-assistant-server` repository owns activation
records, device bindings, expiry calculation, and every server private key.
It must never be copied into a desktop release.

Server-issued activation codes are exactly 256 characters long (`MAL-` plus
252 cryptographically random hexadecimal characters). The server retains only
their SHA-256 hashes, binds one code to one device on first activation, and
starts a time-limited expiry then. An individual code can bind to only one
device, and a device holds exactly **one current code**: activating a new code
detaches the earlier binding, while that earlier code keeps its expiry and its
own remaining auto-lie balance, so it can be rebound later without a new grant.
The remaining balance therefore belongs to the code and travels with it; the
device row is only the aggregate the desktop is shown. Ten failed activations
for one fingerprint within 24 hours ban that fingerprint.

That fingerprint ban is the only ban the client endpoints apply. The source-IP
ban is a separate and independent policy owned by the operator website
(`/forbestop/` and `/console/api/`); `/api/v1/activate`, `/api/v1/validate`, and
`/api/v1/lie-events` never count, clear, or enforce an IP ban. A customer's
failed attempts therefore cannot ban the public address that other customers
share, and an address banned on the website never blocks activation or a
heartbeat. Neither ban can be cleared by the other, and the website policy is
inert until its reverse-proxy address header is configured on the server side.

The activation address is built into the desktop client:
`https://211.149.169.194:8443`. Customers enter only their activation code;
they never enter or choose a server address. Before the activation request body
is sent, the client verifies the public certificate pin in
`activation_server_pin.json`. There is no HTTP fallback.

The server uses `LICENSE_SIGNING_PRIVATE_KEY` only to sign successful license
responses. It is a Base64url-encoded 32-byte Ed25519 private key stored only
in the server environment. The matching public key is packaged in
`license_public_key.json` with each desktop release. Changing the server
private key therefore requires a matching client public-key release; a private
key can never be reconstructed from its public key.

`server_client.log` records safe activation diagnostics: connection start,
certificate-pin outcome, HTTP result, and local acceptance/refusal category.
It never records an activation code, hardware fingerprint, token, or secret.
Automatic lie handling is deliberately *not* in this file: its credential,
accounting and connection lines go to `auto_lie.log` (see *Current release
behavior (v1.2.160 – v1.2.163)*). The linked-node icon in **运行日志** copies
`server_client.log` to the clipboard.

Immediately after a Windows restart, firmware or motherboard CIM/WMI signals
can temporarily be unavailable. This is reported as **设备未就绪，稍后重试** and
is distinct from **激活验证失败**, which means the certificate-pinned request to
the activation service could not be completed. Technical categories remain in
`server_client.log`; neither the raw fingerprint nor a secret is logged.

The most recently submitted code and its validation result are stored in the
`license` section of `user_config.json`. The signed `auth.json` document is
the actual local entitlement, kept in the selected install root's `助手配置`
(`MOONECLIPSE_CONFIG_DIR` when the launcher sets it). Installed copies the
operator upgrades with the separate installer never retype the code: the
installer reads it from that profile (see *Current release behavior
(v1.2.290 – v1.2.291)*). A verified rejected replacement code immediately
locks automation and removes that signed document, so the rejected state also
persists after restart. A connection, TLS, or server-configuration failure is
reported separately and does not erase a previously saved entitlement.

After a successful online activation or heartbeat, the server may include the
auto-lie credential in that already pinned, authenticated response. The client
keeps it only in process memory, refreshes it on the three-hour heartbeat, and
destroys it when the application exits. It is never written to
`user_config.json`, `license.json`, a release ZIP, or a client log — a redaction
filter enforces the last of those even against a masked preview. The credential
remains separate from both signed-license validation and the vendor WebSocket
adapter.

The credential is delivered by the reply and then held; it is *not* discarded
before a request is sent. It is cleared by a failed heartbeat, by a verdict that
the license expired or was revoked, and by the server withholding it (an empty
`auto_lie.secret_key`, which is what a device with no passes left receives). A
successful heartbeat never clears it, and a reply that does not mention
`auto_lie` leaves it alone. **自动过测谎** is unselected and greyed whenever a
heartbeat clears it. Heartbeats themselves are sent only at application startup,
on their own three-hour/midnight schedule, after a failure, and from the caption
**↻** — never because a lie window appeared.

Each successful heartbeat also returns the server-issued eight-character
**设备码**. The authorization line always displays that code (and left-clicking
it copies it). The client preserves the online device state across ordinary
local signature checks, so a routine UI permission check cannot erase it.
When the automatic API WebSocket handshake is ready, the client first stores a
UUID in its durable local outbox, then asynchronously reports `STARTED` after a
random 0–30 second spread. It reports terminal success after marker recovery,
or `failed_marker_missing` only when the character is verified in the black
room. Both terminal reports use their own 0–30 second spread and never block
the live capture or cursor workflow. A normal application close queues success
for any still-pending event and gives the sender at most 500 ms; an unsent row
remains in the outbox for the next start. During a pending event, the server
returns all three counters: `total` has increased while `success` and `failed`
retain their previous values.

The response's additive `autolie_fingerprint_usage` object updates the same
authorization line at once with total, success, failure, and remaining usage.
A numeric remaining balance is authoritative: it replaces the previous value even
when an earlier answer left the unlimited marker behind, and the marker itself is
believed only when the response carries a null count *and* its unlimited flag. A
just-activated limited grant therefore shows its number immediately rather than
无限, and the usage line prints the count before the marker.
The server treats an unfinalized row as success only after a two-minute
server-time grace period at that device's next heartbeat, or after ten minutes
through the stale-row cleanup. Both rules use server receipt time, so a fresh
delayed report cannot be inferred complete early.

The operator console shows an activation code's **设备绑定** as this public
equipment ID rather than a fingerprint fragment. A device memo is a shared,
bounded label: the desktop stores it in `user_config.json`, accepts only
Chinese/English letters, digits, spaces, `_`, and `-` (at most 24 characters),
and asynchronously syncs the same value to the bound device through the
pinned activation service. It is not included in a signed license document.

The same row also contains **解绑**. This is a customer self-service endpoint,
not an operator-console action: `POST /api/v1/device-unbind` authenticates with
the original authorization code plus the device fingerprint, never logs the
plaintext code, and detaches only that currently bound code. It is available
only when `auto_lie_unlimited_mode` is false. The per-device local-day counter
limits successful calls to two; its day boundary uses the activation server's
`Asia/Shanghai` timezone. A success deliberately deletes only the desktop's
local signed document: `user_config.json` retains the code so it can be copied
and used to activate the new device.

An activation code has either **counting** or **unsettled** auto-lie mode.
Counting mode consumes its remaining balance. Unsettled mode leaves the balance
unlimited and increments the code's unsettled counter for each auto-lie start.
The device console exposes mode switch and settlement actions: settlement resets
the unsettled counter and its current usage-window totals, while switching from
counting to unsettled begins a new zeroed window. The desktop keeps the daily
and yesterday counters visible and renders the primary value as either
`测谎 (剩余, N)` or `测谎 (未清算, N)`.

On the console's **授权码** page, the `授权码 / 设备码` search accepts either an
eight-character equipment ID or the original 256-character activation code.
For an activation-code lookup, the browser sends the value in an authenticated
POST body; the server normalizes and hashes it using the activation algorithm,
then finds the matching stored digest and displays its authorization ID. The
plaintext is neither stored, returned, audited, nor put into the request URL.

### Server deployment, updates, and client compatibility

The server lives in the separate `maple-assistant-server` repository. A remote
server agent should work there only; it must not copy desktop code, release
ZIPs, `license.json`, activation inventories, or any private key into that
repository. Its authoritative update guide is that repository's `README.md`.

For a normal server update, commit and push the server repository, then on the
server pull the exact commit, load its `.env`, install changed requirements,
run Django migrations, and restart Gunicorn only after migrations succeed.
Keep the previous deployed commit available for rollback. Check the pinned
`/api/v1/validate` endpoint and `/forbestop/` after every restart; never test a
production update by changing a customer's client endpoint.

The v1.2.252 self-unbind endpoint requires migration
`licensing.0015_fingerprint_self_unbind_limit` before the new desktop client is
used. It adds only the per-fingerprint `self_unbind_day` and
`self_unbind_count` rate-limit fields; it does not alter activation codes,
existing bindings, entitlement periods, or auto-lie balances.

**Existing clients remain compatible only when the server preserves all of
these contracts:** the same HTTPS address and TLS key/pin, the same
`LICENSE_SIGNING_PRIVATE_KEY`, the existing PostgreSQL data, and the existing
versioned `/api/v1/activate` and `/api/v1/validate` response fields. New
response fields must be additive and optional. In particular, an older client
ignores the optional `auto_lie` response object; it must never be made a
requirement for license validation.

If a new server deployment fails, roll back the server code while retaining the
database, TLS key, and signing key. The desktop application validates online
immediately at launch, so a newly started client becomes grey and locked while
the service is unavailable. A client already running from a successful
validation remains active only until its next three-hour heartbeat; a failed
heartbeat locks automation. Therefore restore the compatible server promptly
rather than asking customers to replace their client files. Do not roll back
the database schema by hand; prefer an additive migration followed by a code
rollback that still understands the newer schema.

### Server-agent instruction: auto-lie lifecycle deployment

The server agent must deploy the client lifecycle support together: pull the
server commit, load the existing `.env`, run `python manage.py migrate`, then
restart Gunicorn. This applies migrations `0007_lie_event_lifecycle`,
`0009_lie_event_pending_index`, `0010_activationcode_auto_lie_quota`,
`0011_code_owned_autolie_quota`, and `0012_one_current_code_per_equipment`.
The last two move remaining auto-lie quota ownership to the currently bound
activation code and enforce one current activation code per equipment. Do not
replace the database, TLS key, signing key, or auto-lie secret.

Before restarting, the server agent must back up PostgreSQL and verify the
migration plan against production:

```bash
cd /path/to/maple-assistant-server
. .venv/bin/activate
set -a; . ./.env; set +a
python manage.py showmigrations licensing
python manage.py migrate --plan
python manage.py migrate
sudo systemctl restart maple-assistant-server
```

Then confirm `/api/v1/validate` and `/forbestop/` through the existing pinned
HTTPS endpoint. If a rollback is needed, roll back only the application commit
while retaining the migrated database and all existing keys; these migrations
are additive and the previous compatible server must understand the result.

Install one server-side periodic command, not one job per device. Run it every
five minutes with the ten-minute cutoff:

```bash
cd /path/to/maple-assistant-server
. .venv/bin/activate
set -a; . ./.env; set +a
python manage.py settle_stale_lie_events --minutes 10
```

The scheduler selects only indexed rows still marked `pending` whose
server-side `recorded_at` is at least ten minutes old. It settles each row and
device independently; it cannot create, scan, or settle events the client has
not yet reported.

## Interface layout

The interface calculates each main column from its widest supported content when it is created, then keeps that column width fixed for the rest of the session. Switching attack modes only enables or disables the controls already reserved in the layout; it does not repack rows or resize either column. The window height remains adjustable and is allowed to grow or shrink as content such as quick-message rows changes.

Every hint shown to the operator is Chinese, including the refusal reasons printed under **图层校准与巡逻** — a failed recording names the cause in Chinese (for example 最上层无法录制绳索点, 该楼层尚未录制, 未找到游戏窗口, 巡逻启动时未检测到黄色角色标记) instead of mixing an English reason into a Chinese prefix. Messages that only reach the log or a developer remain English on purpose, so a traceback can be searched for by its original wording.

## Recording a map

Record each map before enabling a route:

1. Record the **left endpoint** of the lowest layer.
2. Record its rope point when the layer has a rope.
3. Record the **right endpoint**.
4. Optionally record directional jump points with `Ctrl+D` (left) or `Ctrl+F` (right).
5. When a map has disconnected patrol areas, record each vertical **split point** with `Ctrl+S`.
   The UI draws it as a wave line through every layer; splits divide the map into left-to-right
   patrol parts.
6. Add the layer above it and repeat.
7. Select the patrol start and end layers.

Layers are stored from bottom to top. A patrol route can cover any contiguous range; a map does not need exactly three layers.

Each layer's row has an axis band beside its name. **Right-click the axis** to open its point menu (添加最左 / 添加绳索 / 添加最右 / 添加左跳 / 添加右跳 / 添加花点); the menu is only available while patrol is stopped, because recording is locked while it runs. Left-clicking does not open the point menu; it only follows normal layer/marker selection behavior.

### Split patrol and skill points

Split points are global vertical boundaries, not extra layers. They divide every recorded layer
into left-to-right parts. A part may omit a layer entirely: that layer is simply skipped in that
part when it has no recorded support point. For each available layer, the assistant chooses the
nearer endpoint as the first endpoint on entry; later passes keep that chosen left/right sequence.
The **巡逻 N 次** menu on the layer row controls how many endpoint traversals finish before the
next available layer or part begins (default: 1).

`Ctrl+E` records a skill point. After pressing it, press one permitted attack key to bind the
skill; passing the point releases that key. `Ctrl+G` records a flower point on the selected rope
layer. The point menu displays separate `花1`, `花2`, … labels even when their X values are the
same, because their distinct Y positions are meaningful.

The minimap detector first finds the actual map border and calculates marker coordinates relative to that border. A broad search rectangle may help locate a map, but it is never saved as map geometry. This matters when the minimap size changes between maps or when the UI temporarily covers part of the game window.

Inside that border the detector also measures the **map canvas** — the map panel's own inner rectangle, below the minimap header and above its footer. It is measured from the captured pixels: the canvas is the border rectangle nested directly inside the window's border, inset on every side, and it is never a fixed height, a fixed bottom offset, or a proportion of the window, because the game does not rescale this HUD uniformly between client widths. A frame whose inner border cannot be read (map artwork covering it) reuses the canvas measured earlier in the same session; when nothing has been measured yet for that client size, no canvas is claimed at all. See *The map canvas and the detection overlay*.

At patrol start, the assistant focuses the game, uses the recorded minimap geometry, identifies the current layer, and either starts that layer’s route or begins the recorded return path. The selected layer is visible in the UI, and optional minimap overlays can show the recorded layer bands for inspection.

### The map canvas and the detection overlay

The startup detection overlay flashes three regions, and only the first is the minimap window itself:

| Colour | Region | Source |
| --- | --- | --- |
| Green | the minimap window | the measured outer border |
| Yellow | the map canvas: the marker and patrol analysis region | the measured inner border |
| Blue | the HP/MP status capture area | fixed HUD geometry |

The yellow rectangle is the canvas the assistant actually reads the character marker from, so it must not wander into the minimap header or footer. It is resolved in this order:

1. the separate inner-border detector's own measurement (including its held previous result);
2. an inner contour the outer minimap pass already found;
3. the pixel measurement described below;
4. the canvas measured earlier in the same session — a few pixels of frame-to-frame jitter keep the remembered box, so the coordinate frame does not shift while the character stands still;
5. otherwise **no yellow rectangle is drawn at all**, and the log says so. Enlarging it back to the whole minimap window is never an option: a rectangle that claims the window (or the broad search region) as the map canvas is worse than no rectangle.

The pixel measurement (`minimap_detector._measure_inner_canvas`) takes the detected window from the same frame, finds its border contour tree, and accepts the largest rectangle nested directly inside that border which is inset on every side, at least half the window's width, at least 15% of its height and at least 20% of its area. Rectangles nested deeper are map artwork or a panel drawn inside the canvas, and adopting one would cut the character marker out of the analysis region. A faint border and a border broken by artwork are covered by two further passes over the same crop; the plain pass is preferred because closing an edge map can merge the canvas with the artwork.

The log states which branch produced the rectangle, once per distinct outcome: `INNER CANVAS: measured (6, 90, 188, 238)`, `INNER CANVAS: reused (6, 90, 188, 238) (this frame's inner border is not readable)`, or `INNER CANVAS: not measurable and none remembered; the analysis region stays the detected minimap window and no yellow marker region is drawn`.

Everything above is display and analysis geometry. The overlay rectangles are converted from capture pixels to screen pixels only for painting (`screen_blinker._capture_pixel_to_screen`), and the logical window rectangle used by game input and the auto-lie cursor workflow is unchanged.

### Directional jump points

A jump point is a locked X/Y record on one layer. `Ctrl+D` records a **left jump** and `Ctrl+F` records a **right jump**; points are sorted by X and can be removed but not edited. During ordinary patrol, a point fires only while travelling in its recorded direction. During an active rope climb, either directional point can fire when the live marker matches it.

Matching keeps the horizontal window tight (±0.010 minimap units) and allows a wider vertical window (±0.020) so a rope approach can still meet the point. The resulting motion holds the recorded horizontal direction, presses jump, and holds Up until three later marker samples show that Y has settled on a horizontal platform.

A point fires **once per leg**, not once per entry (v1.2.20). A leg is one directional traversal of one route floor, identified by the Start Patrol pass, the route floor, and the route phase; walking the same leg again is the same leg. Leaving the X/Y window no longer re-arms the point, because that window is deliberately loose — a landing, a walk back over the same spot, or a climb past the same row re-enters it at once, which is how a 右跳 that had already mounted the rope at layer2 fired the same 右跳 again on another run and threw the character off the rope it had just grabbed. A climb may use a point at most once per pass, and a 0.4 s refire cooldown absorbs a marker flickering across the zone edge. A new leg, or a new Start Patrol, arms every point again.

The running log states all of it: `JUMP POINT pass 5 started (…): recorded points re-armed (start patrol)`, `JUMP POINT fired: layer1[0] is now used for leg pass=5 floor=0 phase=right`, and `JUMP POINT suppressed: layer2[0] right jump - already fired on this leg` (or `… already used in this pass (climb)`) when the guard is what stopped a second jump.

At patrol start, all recorded points are displayed together for five seconds: blue bars for endpoints, a yellow up arrow for a rope, and thin green directional arrows for jump points. These overlays are diagnostic only and never participate in movement decisions.

When a jump point holds Up into a rope climb, the climb's own Up hold and gate are left alone (v1.2.21): the jump-point session releases only its own key claim, so a settled landing Y cannot un-gate attacks or 小碎步 while the character is still attached to the rope.

The Up claim a point takes is itself bounded (v1.2.280). Its normal exits are a settled landing on a higher floor, a rope handoff, or a climb state that still owns Up; a point that fires and **fails** — the character comes back down on its departure floor — reaches none of them, so a claim that produces no landing, no handoff and no rising marker is released after `JUMP_POINT_SESSION_MAX_SECONDS` (3 s) with `JUMP POINT: released a N.Ns Up claim that never produced a landing`. Every forced key release drops the claim together with the key, because `_release_stuck_keys()` — the path used by a Start Patrol, a confirmed fall, and the global `INPUT RESET` — forgets the claim in the same breath as it releases Up; a new movement pass cannot inherit the previous pass's claim either. Without those rules a dead claim outlived its jump and replaced every later climb with `action=wait (recorded directional jump point owns Up; waiting for landing)`.

After a fall, a stale session is handed over as soon as two downward marker frames *and* a settled reading strictly inside the lower floor's band prove a landing (`_track_fall`); ordinary fall recovery keeps its own three-frame requirement, so a normal jump arc never loses its Up claim.

### Layer bands and one-supporter layers

A layer may legitimately record a single supporter — a rope point only, or a left-most or right-most endpoint only. Such a layer keeps the one-sided band `[y, y + small_gap]`, and `small_gap` is now `max(y_tolerance / 3, 1 / analysis_height)`: one marker row (`0.007143` on the 114×140 minimap), never less (v1.2.20). The old fixed `0.002` is 0.28 px, smaller than the marker's own one-pixel quantisation, so it could only match a reading that landed on the recorded row exactly — a character walking that very floor read `matches no band`. Jump points never widen or shift a band; they are not patrol supporters.

The same rule lives in both `movement_worker.py` and `patrol_control.py`, and the startup line says which shape produced each band: `LAYER BAND: layer1 y=(0.753571, 0.760714) width=0.007143 (single supporter: [y, y + small_gap])`.

A one-row band is a reference row, not the whole floor: on a floor whose canvas Y changes along its length (a bench or stepped platform), readings away from the recorded row still match no band, and the floor is identified through the scroll-compensated world-Y band instead. A layer with one supporter has one route phase, so it can only repeat that single point; a floor that must be patrolled end to end needs its endpoints recorded.

## Patrol, movement, and combat

### Patrol safety

- Movement is sent only while the selected game window is safely foregrounded.
- Left, right, up, and down are serialized. Direction switches release the old direction before arming the new one.
- Patrol, rope return, recovery, small-step, buffs, and special motions use coordinated input ownership so a stale key cannot keep the character moving.
- Every vertical claim is released together with the physical key. A climb or jump-point claim that outlived its key press is what replaced later climbs with `action=wait`: a forced key release now drops the claim (`_forget_up_claims`), a new movement pass cannot inherit one, and a jump-point claim that never produced a landing is bounded to 3 s.
- A queued buff is not discarded merely because a short stationary return temporarily owns the movement stage. It remains queued until that return settles; only an actual Stop Patrol clears queued buff motions.
- If patrol has no recorded route, the assistant remains in a safe stand-still state and combat functions can still operate.

At a patrol endpoint, TodoHelper performs a movement-owned turnover. As soon as
the marker reaches the left-most or right-most arrival band, it releases the
old direction, reserves a 100 ms neutral input interval, then sends the one
opposite direction selected by the next route phase. Attack input is blocked
through that neutral interval and a 60 ms post-send guard. The reverse key is
sent immediately after the neutral interval even if a prior attack is still
animating, so a turnover never waits through an attack cadence. If that first
key-down is swallowed, the first capture after the prior attack ends becomes
the X baseline; on the following 5-FPS capture, no reverse movement of at
least 0.003 X causes exactly one fresh reverse-key re-arm (20 ms key-up gap).
The hold manager preserves that reverse hold until this verdict, and no second
attack may enter the handoff. This keeps endpoint turns responsive while
preventing attack, jump, or stale walk input from swallowing the reverse.

### Returning to the route from a lower floor

A return climb heads for the recorded floor directly above the floor it is climbing from, capped at the bottom of the patrol range — not for the next route layer, which sits one floor too high while the character is below the range (v1.2.21). The arrival test uses that floor's band widened by one marker row on both sides.

While the character is already attached to a rope, TodoHelper also watches the
raw minimap position. If both X and Y remain unchanged for four fresh captures
away from a confirmed platform arrival, it treats the climb as stuck (for
example, an Up hold released during a focus interruption) and restarts it with
an `Alt+Left` climb jump. The rule does not run during next-layer arrival
confirmation, where an unchanged marker is expected while stepping off the
rope.

A recorded floor **outside** the patrol range that the climb has climbed up to is a real arrival, not a stall: the return rebases on it (`RETURN TO ROUTE: still on layer2 outside range; climbing back`) and continues from that floor's own rope. This is what ends the loop where the character stood on layer2 at the platform top, world Y stopped advancing, and the climb detector released Up and re-jumped forever.

A frame in which no recorded floor matches while a climb owns Up is the character's own jump arc just above the platform row, not evidence against the arrival: up to three consecutive such frames are tolerated and the arrival confirmation survives them (`CLIMB_ARRIVAL_BLANK_FRAMES_TOLERATED`). Four or more still restart the climb.

A floor without a recorded rope no longer inherits the legacy profile-wide `rope.x`. The climb target is that floor's own recorded rope, or — when there is none — its recorded jump point nearest that legacy X, which is the operator's own way onto the rope: `ROPE TARGET: layer2 has no recorded rope; using its recorded jump point x=0.469298 as the rope approach`. The legacy `rope.x` walked the character to a rope that floor never recorded (on layer2 it went off the platform and dropped to layer1). When a floor has neither a rope nor a jump point, the return stands still and logs it instead of walking to the legacy X.

A recorded rope is exact: its saved normalised X/Y is used as the climb target without canvas, diamond-size, preset, or smoothing projection. This prevents an unstable live minimap measurement from moving a correctly recorded rope sideways into an unreachable air-rope target. Record the rope directly under the real rope; the assistant uses that same position.

A return is still a return: the patrol range selected in the layer panel decides which floors may be patrolled, and an out-of-range floor can only ever be used as the way home. A floor used as a waypoint needs its rope — or at least the jump point that mounts it — recorded.

### Dropping back into the route from a higher floor

A return drop (a reconnect or a fall that left the character above the patrol range) ends only when the landing floor is recognised, and it is recognised from the marker:

- The marker's own recorded band is direct evidence and is accepted immediately, exactly as before.
- A landing away from the recorded row matches no band at all — the operator's own map: layer1's points were saved at 0.676829 while the character stands at 0.713415 further down the same platform. The same marker-only answers ordinary patrol already uses resolve it: first the bottom recorded floor when the marker reads at or below its band (nothing is recorded lower, so the descent cannot continue; `_finish_return` then either patrols that floor or climbs back when the patrol range starts above it), then the bounded nearest recorded floor over **every recorded floor** — not only the patrol range, because a single-floor range would otherwise leave the floor the descent is aimed at out of the question (v1.2.280).
- Both relaxed answers need real drop evidence first: at least one Alt+Down chord actually sent, and the marker moved down by at least `RECONNECT_DROP_Y_PROGRESS` (0.015, one and a half marker rows) from where the descent began. They also need the reading to be **settled**: a character falling *through* the floor sweeps the marker down the minimap (the 14:38 log ran 0.372 → 0.397 → 0.409 → 0.445 → 0.482, about 0.027 per frame) and must never be read as a landing. The answer must hold for two consecutive settled frames (`DROP_ARRIVAL_CONFIRM_FRAMES`).
- The scroll-compensated world-Y tracker is deliberately **never** consulted for the landing: at a fresh start above the route its origin is anchored to the route's *top* floor while the character is still above it, so it would end the drop before the first Alt+Down chord. Since v1.2.280 the same distrust applies globally after 自动重连: the world-Y origin is re-taken only from a marker-confirmed floor.

The drop itself is bounded the same way a standing character's layer change is (v1.2.280): two Alt+Down chords without downward progress send the character to the nearer outer edge (X > 0.5 → left, X < 0.5 → right) to look for a drop edge, then the opposite one, and the recovery abandons after three cycles or a 30-second window. A marker that matches no recorded floor at all is handled by the unresolved-floor recovery below.

The landing evidence belongs to one descent. It is cleared on a fresh patrol start and once the return mode ends, and it survives a frame in which the marker is momentarily lost so one missing reading cannot restart the measured descent. The log names the floor and the evidence: `DROP TO ROUTE: accepting layer3 as the landing floor (marker y=0.408000 matches no patrol-floor band; the drop moved the marker 0.078000 down and the reading held for 2 frames); restarting patrol`.

### Unresolved floor (no recorded layer matches)

A marker that matches no recorded band and is farther than the nearest-floor allowance (`LAYER_NEAREST_FLOOR_MAX_DISTANCE`, 0.05) from every recorded row leaves the worker with no floor at all: the route has no target, the 站桩 recovery refuses to act (`marker outside anchor layer band`), and the character would simply walk a platform nobody recorded. The 17:46 field log is that state — `action=right` while every frame was refused as `0.1284 away from layer2`.

The floor is therefore changed deliberately (v1.2.280), with the reconnect respawn's own shape:

- Alt+Down repeatedly while the character stands still. Each chord is assessed once `drop_retry_seconds` (1.5 s) has passed; a descent of at least `RECONNECT_DROP_Y_PROGRESS` (0.015) clears the stall count, so a real multi-floor descent keeps dropping.
- After two chords without downward progress, walk to the nearer outer side for `UNRESOLVED_EDGE_HOLD_SECONDS` (5 s): **X > 0.5 → left, X < 0.5 → right**, the same rule the reconnect respawn uses. That side's platform edge is the one the character is closest to. A descent during the walk returns straight to Alt+Down.
- The opposite side is tried next. If neither edge descends, the chords resume; after `UNRESOLVED_DROP_MAX_EDGE_CYCLES` (3) cycles or `UNRESOLVED_DROP_MAX_ATTEMPTS` (12) chords the recovery stops and logs why, instead of hopping on one spot forever.
- Four settled frames are required before the first chord (a falling marker sweeps the minimap and is never "settled"), and the recovery yields to every other vertical owner: a return, the planned descent to the first layer, an attached climb, a jump-point Up claim, the reconnect drop recovery (only while its `drop-to-route` return is actually running), a pickup leg, or a pending post-reconnect route check. The log names the owner once per change: `UNRESOLVED FLOOR: layer change deferred while the drop-to-route return is over` / `… while an attached climb holds Up`.
- The moment the marker resolves, everything resets and normal routing resumes: `UNRESOLVED FLOOR: the marker resolved as layer1 after 2 layer-change attempt(s); resuming normal routing`. A fresh Start Patrol also resets it.

A reading that is merely *between* two bands but within the nearest-floor allowance of a recorded row is a marker alias, not a foreign floor, and never starts a drop.

### Attack modes

**巡逻攻击** performs the configured attack key at its selected fixed interval. **小碎步** is an optional timed motion with its own interval.

**小碎步** is an atomic left/right correction. Each direction is held for 220 ms with a 100 ms neutral gap; normal patrol resumes only after the sequence completes.

**站桩攻击** captures the character’s current location each time patrol starts. It is independent of recorded map layers and uses that temporary position only for its own local recovery. **跳打**, **小碎步**, and **朝向** are available in this mode. 朝向 can be left, right, or 双向; 双向 flips the required facing every 80 settled minimap frames through a short atomic facing tap. Patrol start itself does not issue a facing tap.

The attack-delivery choice is **站打**, **跳打**, **蓄力**, or **不打**. 站打 taps the configured attack key, 跳打 performs the established jump-then-attack sequence, and 蓄力 holds the configured key for a random inclusive value between its two displayed seconds values before releasing it. During a charge, ordinary patrol Left/Right can still run, but other action-key motions wait; HP/MP recovery can interrupt the hold. Selecting 不打, or setting the attack key to `-`, suppresses normal attack output.

**死花模式** is separate from combat patrol and sends only buffs plus HP/MP items. With
**重置花** unchecked it never moves, regardless of recorded flower points. With **重置花**
enabled, one flower point means attach to the rope, buff there, then leave the rope for the reset
circuit before returning. Two or more flower points mean climb between their distinct Y positions
and refresh Buff1 at each timer boundary. Normal attack controls and 小碎步 are unavailable in
this mode.

Numeric settings use the shared compact direct-value control rather than a slider. Hover enlarges a
value; the mouse wheel changes it by one configured step; left-click changes it by ten steps upward;
right-click changes it by ten steps downward. Range controls enforce their ordered inclusive bounds.
The fixed **attack interval** remains a `0.20–30.00s` range with `0.05s` precision, so `0.25s`
remains `0.25s` rather than being rounded to `0.3s`.

HP and MP potions have priority over ordinary combat actions. Pet food is treated as a timed consumable and does not reserve movement input.

### 被撞反击 (hit reaction)

**被撞反击** is an event reaction, not a cadence: it is a checkbox with its own
**攻击键** binding on the 巡逻攻击 row (stored as `counterattack_enabled` and
`counterattack_key` in `fixed_attack_settings.json`, the key defaults to `ctrl`).
It works in both 巡逻攻击 and 站桩攻击 while patrol input is armed, and switching the
attack mode off disables it with the rest of the combat controls.

Turning **被撞反击** off is final for pending work as well: TodoHelper removes
unstarted reactions waiting for an arbiter or marker sample and clears an
unspent stationary replacement slot. It never interrupts a key hold that is
already physically in progress, but it cannot reserve a later movement or
replace a later normal attack after the checkbox has been cleared.

1. **The HP bar is the trigger.** `status_worker.py` owns the reading: only an
   action-grade sample (confidence at or above the action threshold) that is
   *lower* than the previous action-grade sample counts as a hit. A one-frame bar
   wobble in a weak read therefore cannot press a direction key.
2. **The hit-frame X is retained.** Status and minimap analyses run on independent
   threads, so the reaction uses the marker X recorded for that exact capture
   sequence in a twelve-sample window. In stationary mode this only replaces the
   next existing attack slot with the bound counterattack key; it adds no step.
3. **Patrol returns toward a compensated hit before attacking.** A clear X
   displacement keeps the existing 0.10 s step against the knockback. When patrol
   movement masks the displacement (< 0.003 X), the later reaction instead steps
   toward the saved hit X, then taps the bound key in the same atomic motion.
4. **It never overlaps anything else.** The reaction is queued through
   `motion_arbiter.py` as its own motion type, so it cannot run during a climb,
   drop, stair jump, buff, 小碎步, or an anchor correction; the step releases the
   ordinary walk hold first. Only one reaction may be queued at a time, so the
   several frames of one hit produce one reaction rather than a queue of delayed
   walks. The tap is also registered as an attack, so the fixed cadence does not
   add a second beat on top of it.
5. **In 站桩攻击 the reaction owns no movement at all.** The hit is placed into the
   shared one-use `AttackSlot` (`attack_slot.py`), and the next approved beat —
   the fixed cadence, the anchor correction that carries the stationary return,
   or 小碎步's middle attack — spends that key instead of its own. Nothing is
   queued behind a correction, and an unconsumed hit is kept until a beat uses it.

The three-frame cooldown begins when the HP fall is decided, preventing the four-frame game invincibility sequence from producing repeated reactions. The log names each outcome:
`被撞反击 queued as next stationary attack slot: HP 4210->4188 frame=81234 key=ctrl`,
`被撞反击 queued immediately: HP 4210->4188 frame=81234 compensated walk left (Δx=+0.000595) -> right + ctrl`,
`被撞反击: returning toward hit X 0.469048 from 0.462000 -> right`,
`被撞反击 executed: attack only + ctrl`, and
`被撞反击 skipped: HP-drop cooldown through frame 81237 (current 81235)`.

The hover hint on the checkbox is the operator-facing warning: 建议在怪物少的地图使用，否则频繁被撞影响正常输出。

### Position correction while standing

站桩攻击 holds the recorded spot with one atomic motion per correction: a short direction hold — the tiny step inside the inner band, a longer walk when the character is further out — followed immediately by one attack. The attack belongs to the correction, so a correction can never be the moment a beat is lost; corrections repeat until the marker is back inside the arrival band (±0.006X), spaced by the arbiter's own correction reservation (**0.60 s**, shared with 被撞反击) rather than by the shorter 0.30 s re-request interval. A drift is always walked back: the character never settles a pixel or two away from its 桩. The small-step band ends at ±0.010X, and the correction spacing only spaces the corrections — it does not create a gap in which a beat is skipped.

When a normal return walk starts outside the final approach band, it first waits
for an already-running attack animation to finish. Sending its initial Left or
Right during that animation can be ignored by the game; the old hold manager
then kept extending a direction the game had never accepted, while return
protection suppressed every later attack. The clean-tick gate prevents that
stationary-return freeze without changing the ordinary walking path.

That gate covers the first key-down of a far return only. A key-down swallowed
later in the same walk — a hit reaction or a cast can consume it — is caught by
the progress watch instead: after `STATIONARY_RETURN_REARM_NO_PROGRESS_FRAMES` (3
captures, about 0.6 seconds at 5 FPS) in which the marker does not close on the
temporary anchor, the walk hold is released and the same direction is armed
again (`STATIONARY RETURN walk made no X progress toward anchor for 3 frames
(distance=…); releasing and re-arming left`). A direction that is really walking
keeps its hold: 0.001 X of progress restarts the watch.

Two related rules protect that motion:

- While the optional **小碎步** pair is queued or running, nothing is queued behind it. The pair deliberately steps away from and back to the selected 朝向 and sets the facing itself when it finishes, so a correction or facing tap queued during it used to land as an extra step (and an extra attack) immediately after 小碎步.
- The **朝向** tap is a 30 ms turn. It is long enough for the game to turn the character and short enough to stay inside the anchor band; a longer hold walks the character out of the band, and the correction that answers it turns the character back — the face/walk twitch.

### 定时巡逻 (stand-still timed route)

**定时巡逻** is available in 站桩模式 and 双桩模式. When its timer fires from a settled 桩, it borrows the selected recorded patrol route, patrols every route layer exactly once with the normal rope/jump/drop logic, then returns to the anchor layer and the nearest valid 桩. It sends no `Z` pickup input while moving. Its inclusive random interval is displayed in minutes (`每 … m`, 1.0m to 30.0m); persisted configuration and the movement worker continue to use seconds.

## Alerts and recovery

The additional-functions panel can enable sound, screen flash, Telegram messages, disconnect alerts, lie-detection alerts, timer alarms, and other-player channel switching.

### Patrol self-rescue probe

Self-rescue is guarded so a normal attack animation cannot be mistaken for a
genuine movement stall. After 25 consecutive near-unchanged minimap captures
(about five seconds at the shared 5 FPS cadence), the movement worker blocks
attacks for 2.0 seconds and performs a bounded probe: right for 700 ms, wait
200 ms, then left for 700 ms and wait 200 ms. If the live marker moves by at
least 0.006 X during either nudge, the character is not stuck and no
drop/restart is attempted.

The probe owns only movement input; it does **not** stop the shared patrol
controller. Capture and marker tracking continue normally, while ordinary
route input yields to the probe for at most 1.8 seconds. Therefore a rejected
probe does not show `巡逻已停止`, does not enter `patrol-paused`, and does not
turn a normal attack handoff into a misleading `wait` action. Only a confirmed
stuck condition can proceed to the existing recovery path.

Every optional alert control has a hover explanation: **循环** raises an alarm
when its remaining time reaches zero; **声音** plays the dingdong sound;
**闪烁** flashes the screen red for machines without usable audio; **消息** sends
a Telegram notification; **自动重连** reconnects after a verified disconnect;
and **自动测谎** hands the mouse to the automatic lie workflow and consumes one
lie-detection use even if it is interrupted with Esc. Leave 自动测谎 off when
manual lie handling is intended.

The compact **循环** controls use an integer interval of `1–300m` and three
independently editable `HH:mm:ss` remaining-time values. Editing a clock part moves the live
deadline without stopping the countdown. **清理内存** is available in the panel: at 98% system
memory it optionally sends the configured offline message, signs out, and hands the game to the
normal reconnect workflow.

When a lie window is detected, TodoHelper targets the first frame upload for
**three seconds after that detection**, not three seconds after the WebSocket
handshake. Connection, capacity preflight, and the formal handshake happen in
parallel with the game's transition; if they finish late, frame upload starts
immediately rather than abandoning the pass. It first uses the safe preset ROI,
then detects and locks the dialog's native dark-red (`#db231b`) border as soon as
that border is visible. This keeps the sent crop tied to the real playfield on
both 1366×768 and 1080×768 clients instead of assuming a fixed minimap-size
rectangle while the dialog is still animating in. A newly recreated game window
is classified by its process identity, not its HWND alone: a replacement inside
the same running game process receives a short five-second surface-settle guard,
while a genuinely new process keeps the 30-second white/black login guard.
Neither guard changes the **自动测谎** selection. That selection is mouse-only,
so global Space/Enter input cannot accidentally toggle it while a stalled game
briefly leaves the dashboard foreground.

Telegram uses one bot token per TodoHelper configuration, but multiple helpers
may use the same token and send to the same chat. Give each helper a distinct
**设备名称** through its normal left-click setting dialog so alerts identify the
originating machine. The token button shows **已设置** when configured. Network
delivery first uses the Windows system HTTP proxy, then probes the usual local
HTTP proxy ports (`7890`, `7891`, `7897`, `1080`, `10808`, `10809`, `8888`,
`8889`), and finally tries a direct Telegram connection. Therefore a local
Clash/Mihomo/V2Ray/ShadowSocks-style HTTP proxy is sufficient; no proxy setting
is required inside TodoHelper.

The book icon in the title bar opens the lightweight teaching viewer. It loads
the JPG pages in `teach-assets/` on demand, in natural filename order; the
release builder copies that folder unchanged into every normal and standalone
package.

Disconnect handling deliberately avoids treating every missing minimap marker as a disconnect. At the shared 5 FPS capture rate, the marker must be absent for 50 samples (about ten seconds), **and** the current frame must show either the login-page evidence or the dedicated offline prompt. The prompt check remains valid when the prompt covers the login background. In minimap-less zones, monitoring pauses after the absence threshold and resumes when the marker returns; it does not repeatedly reconnect or stop patrol merely because a minimap is hidden.

Automatic reconnect begins only after this verified offline event. It dismisses the offline prompt when present, reaches the login screen, opens the chosen world with the guarded double-click sequence, selects the target channel, and waits for sustained marker recovery before patrol can resume.

**自动重开** is a separate 30-second system-memory guard. When enabled and Windows memory use reaches 98%, it sends the optional **重开消息**, signs the character out, and waits until the old game process has completely exited before using the existing `launcher3.0` launcher window. While the new client starts, the assistant restores its focus once per second and waits for the login-page detector before handing off to the normal reconnect workflow. Restart progress is shown in the 图层校准与逻辑 status line; its control and 重开消息 button are on their own line in 附加功能. The authorization header always displays the latest system-memory reading, refreshed on this same 30-second cadence whether 自动重开 is enabled or not.

### Switching channel when another player is present (有人换线)

With **有人换线** selected, a red diamond on the minimap counts as another player,
and the next capture must show it again before anything happens, so one hit flash
or a ragged terrain edge cannot start the workflow. This is the only part of the
assistant that changes channel by itself.

1. It stops the patrol and watches inside a three-minute presence window, sending
   the optional **求让消息** at a random 30–60 second point in it. The other player
   leaving during that window ends the whole workflow — no message is spoken into
   an empty channel, and the patrol is handed back.
2. If the player is still there, it waits the configured **多等** time — entered in
   **seconds** on its own row (0–1200, default 300) — then runs the channel route:
   the recorded **房间码** turns the current channel into a deterministic next
   channel, and without a code the route is random. The row's hover hint is what
   waits for: 确定换线以后，再等 N 秒换线, which is how a 组队 pair leaves one after
   the other.
3. The change is proven by the client's own loading screen: the minimap and the
   yellow marker disappear, then the marker comes back on the new channel. That is
   the moment the 频道 field and the saved channel follow the change — whether or
   not somebody is on the new channel.
4. Another player on the new channel means the route switches on to the next
   channel; an empty one resumes the patrol — but only after the coordinator has
   re-anchored a fresh map/layer session, the same preparation the reconnect path
   uses, so a character that landed above the recorded range drops back into the
   route before any horizontal walking starts. If that preparation fails, the
   patrol stays stopped and the log states it. A route whose keys were swallowed
   (the marker never left its channel) plans the same target again, and after
   three such attempts on one target the route moves on.

The running log names each step: `player channel switch: the map went away for 2
capture(s) - the channel change is in progress; waiting for the marker to come
back`, `player channel switch: the marker is back on the new channel - the change
succeeded`, `player channel switch 3 done: the map reloaded onto channel 22 and it
has no other player; resuming patrol`, and `switch attempt 2 never left channel 29
(the marker stayed visible); 29 -> 50 is planned again`.

Esc cancels the workflow. A cancelled or failed workflow leaves the patrol stopped
and the character standing until **开始运行** is pressed; only a mission that became
unnecessary (the other player left) hands the patrol back on its own.

## Lie detection and automatic handling

The lie detector shares the normal 5 FPS capture cadence. Diagnostic evidence
recording does too: when a lie window, disconnect, or countdown is detected, the
recorder saves copies of those same shared frames as JPGs under `screenshots/`
and later creates its MP4. It does not open a second high-frequency game-capture
loop. Disk writes run in the background through a bounded queue; if they cannot
keep up, old diagnostic frames are dropped rather than delaying patrol, UI, or
automatic lie handling. When automatic lie handling is enabled:

1. Application startup performs a background, no-billing WebSocket probe: it walks the six service instances in a random order and caches the first that answers, together with the whole order for failover.
2. On a lie event, the assistant opens a fresh authenticated connection while the game completes its visual transition — on the cached instance first, moving to the next whenever a handshake fails.
3. The visual settle period is three seconds from event start.
4. Cursor-target frames are JPEG-quality-90 **RTF1** binary WebSocket packets:
   a compact JSON metadata section plus raw JPEG bytes, with no Base64 expansion.
   At 5 FPS, a bounded burst window keeps at most three frames in flight; one
   worker remains the sole WebSocket reader/writer and matches responses by `frame_id`.
5. A full window is back-pressure, not a failure. The pass stops capturing while
   the server returns frame results, keeps the next `frame_id` intact because the
   IDs must stay contiguous, and reports `RTF1 队列已满，等待后端返回（n/max）` once per
   second. Silence beyond `FRAME_RESULT_STALL_SECONDS` (2.5 s) aborts the pass
   before the service's ten-second no-frame timeout closes the socket, and the run
   is recorded as `connection_interrupted`.
6. The **测试API** video drill performs that same authenticated RTF1
   handshake at video start, waits the three-second visual-settle period without
   uploading, then records its handshake timing and transport in its run report.
   Its panel button is hidden (v1.2.163); the drill is unchanged in
   `api_lie_video.py` and comes back by uncommenting three statements in
   `ui_worker.py`.
7. The live pass is ended by the outcome rather than a fixed 15-second frame
   window. From ten seconds after feeding begins through the 40-second deadline,
   the assistant looks for two consecutive captures containing the blue 40×40
   success area at 90% coverage or more. A confirmed success stops the stream,
   clicks the measured confirmation point, and resumes the patrol.
8. Accounting follows that lifecycle, not the video: a pass reports `STARTED`
   once its handshake is ready. The failure outcome `failed_marker_missing` is
   written only when the character is really in the black room — the marker stays
   absent for fifteen fresh captures. Esc or closing the app leaves the event
   pending instead of inventing a failure; the manual **测试API** drill keeps its
   immediate-success accounting because it is an operator-run check rather than
   a live pass.
9. A server-closed WebSocket does not immediately end the result check. The
   assistant retains capture through the same 40-second window. If no visual
   success or black-room failure appears by that deadline, it records success by
   policy, resumes patrol, and does **not** send an unsafe fixed confirmation
   click. The detector then ignores the old square until it disappears, which
   prevents duplicate billing while allowing the next real lie window to start.

The purple detection area and one-shot target indication are visual feedback only. They are not used as tracking input and do not block target chasing.

`autolie_api/` is vendor reference material. Do not edit files inside that directory.

## Quick messages and hotkeys

Quick messages are ordered by creation time. Click once to copy, double-click to send to the game, and hold for one second to edit or delete. Long messages are shortened with an ellipsis in the UI and display their full content in a tooltip.

| Hotkey | Action |
| --- | --- |
| `Ctrl+1` … `Ctrl+0` | Send quick messages in displayed order. |
| `Ctrl+Left` / `Ctrl+Right` | Record left / right endpoint. |
| `Ctrl+Up` | Record rope. |
| `Ctrl+D` / `Ctrl+F` | Record a left-moving / right-moving jump point. |
| `Ctrl+Down` | Select the next layer. |
| `Ctrl+Home` | Select the patrol start layer. |
| `Ctrl+Insert` / `Ctrl+Delete` | Add / delete the highest layer. |
| `Ctrl+[` / `Ctrl+]` | Adjust attack base interval. |
| `Ctrl+\`` | Start or stop patrol. |
| `Ctrl+Z` | Undo the most recent recorded/configuration change while patrol is stopped. Keeps up to 10 in-memory steps; it does not persist across restart. |

Hotkeys have cooldown and hold protection so a single held key does not repeatedly trigger actions.

## Logs and troubleshooting

The in-app running log is the first place to check. Critical unexpected errors
are also written to `error.log`. For an activation problem, use the linked-node
icon in **运行日志** to copy `server_client.log`; it contains the safe
client–server diagnostic trail.

For a failed patrol start, check in this order:

1. The correct game window can be focused.
2. The minimap border and yellow character marker are visible.
3. The relevant layer endpoints and rope point have been recorded.
4. The patrol start/end layer selection is valid.

The status line under **图层校准与巡逻** states the refusal reason in Chinese, so
repeat the step it names (record the missing point, walk onto the selected
floor, bring the game window forward) rather than only pressing 开始运行 again.

When the assistant itself does not start, use the launcher's own log:

| Package | Where the failure is recorded |
| --- | --- |
| normal | `assistant-launch-error.log`; a missing library is installed automatically once |
| standalone | run `诊断启动.bat` and read its console output |

If a hotkey does nothing, the log names the cause: an active IME can swallow the
`Ctrl` chords (`hotkey IME state: ON`), and the fix is printed with it — switch
the IME to 英数/半角英数, or set `"delivery": "hook"` in `hotkey.json`.

For a minimap-less area, return to a normal map before expecting patrol or disconnect monitoring to resume.

### Runtime timing and diagnostic logs

The movement worker still evaluates every shared 5 FPS capture. Its readable
status log is diagnostic but bounded: an action, target, route, or resolved
layer change is written immediately; an X or Y movement of at least `0.003`
from the last emitted position is written immediately with `[moved]`; and a
truly unchanged state is sampled every three seconds with `[steady]`. Existing
stuck, no-progress, nearest-layer, and fallback warnings are never suppressed.
This keeps short knockbacks, oscillation, rope motion, and stationary-return
progress visible without writing five identical lines per second.

Ordinary cadence attacks keep release-grade ownership diagnostics in two lines
per attack rather than four:

```text
attack repetition: a; key-down=a owners=1
key-up=a owners=0; key hold complete=a actual_hold=0.078s
```

This preserves the press/release ordering, remaining owner count, and measured
hold duration needed to diagnose overlapping keys. Failed delivery, lost
focus, forced release, and ownership conflicts continue to log immediately.

Urgent Tk work is ordered as **pre-armed Stop Patrol → hotkey actions →
automatic-lie dispatch**. Heartbeat/accounting/channel/other-player and
reconnect result presentation runs afterward. Those handlers stay on Tk
because they update widgets, but their configuration or display work cannot
postpone a newly armed automatic-lie pass or the immediate Stop Patrol path.

For a delayed hotkey or automatic-lie start, search the main log for:

- `TK EVENT LOOP GAP:` — Tk began a scheduled poll late.
- `UI POLL timing: total=` — one complete poll took too long.
- `UI POLL timing: phase=` — the named poll substep exceeded its budget,
  including `prearmed-hotkey-stop`, `hotkey-actions`, and the two
  `auto-lie:` phases.
- `自动过测谎 timing: detector-to-UI-dequeue=` — direct detector-to-Tk delay.

When reporting a delay, include each matching line and roughly fifteen lines
before and after it so the preceding operation is visible.

## Development and releases

**Release policy: never create a release unless the operator explicitly says
to release it.** This applies to both the normal ZIP and the standalone EXE
ZIP; ordinary code changes remain in the workspace until a release is
requested. When requested, the normal release command is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

The release script advances `VERSION`, creates `release/TodoHelper-<version>.zip`, and removes the previous release ZIP. Field testing is the normal validation path; do not run broad unit-test suites unless specifically requested.

Pass `-Version X.Y.Z` to publish an exact version without advancing it, and `-Minor` / `-Major` to move the second or first part.

The standalone package is built from that staged normal package afterwards, with the same version:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_protected_release.ps1 -Version X.Y.Z
```

Run the two commands in that order: `release_now.ps1` prunes `TodoHelper-*.zip`, which also matches the standalone package name. The standalone build mirrors the staged package's data layout, installs any missing build requirement automatically, compiles from inside the staged package, refuses to publish an executable that cannot import a bundled module, and writes `release/TodoHelper-release-<version>.zip`.

EXE releases are a packaging operation, not a debugging pass. Run the reusable build quietly and report only completion and the ZIP path. Do not inspect or relay build/debug output unless the operator reports a failed launch or another field problem and asks for investigation.

### Release-to-Git mapping

Each shipped version is committed and marked with a Git tag in the form
`release/vX.Y.Z`. The tag is the authoritative mapping from a user-reported
version to its exact source snapshot; use `git show release/vX.Y.Z` when
investigating an older build. Tags are created after the release ZIP is built,
and must not be moved after publication.

A version stays untagged while its source and its built package differ. The first
`release/TodoHelper-1.2.112.zip` package was staged before
`COUNTERATTACK_COOLDOWN_FRAMES` was raised from 2 to 3, so `1.2.112` was left
untagged; both packages were then rebuilt from the commit this table tags —
`release/TodoHelper-1.2.112.zip` by `release_now.ps1` and
`release/TodoHelper-release-1.2.112.zip` by `build_protected_release.ps1`, in that
order, because the normal release prunes `TodoHelper-*.zip` and would otherwise
remove a standalone package built first. The `1.2.105` – `1.2.111` packages were
superseded and pruned, so their exact sources are not recoverable from a version
number alone.

| Release version | Git tag | Status |
| --- | --- | --- |
| `1.2.163` | — | 测试API button hidden again, and both auto-lie missing-credential hints removed. **Untagged: the working tree is not committed.** |
| `1.2.162` | — | Focus loss pauses instead of ending a patrol, a hung game's DWM ghost is not a focus loss, all automatic-lie logging moved to `auto_lie.log`, and the credential can never reach a log file. **Untagged.** |
| `1.2.161` | — | A 站桩 knockback starts walking back immediately: the pre-emptive wait for an in-flight attack before the first return key is gone. **Untagged.** |
| `1.2.160` | — | 站桩 tuning: the arbiter step lock is 0.60 s and shared with 被撞反击, the near correction hold is 0.11 s, and the final approach band is ±0.030 X. **Untagged.** |
| `1.2.112` | `release/v1.2.112` | 被撞反击 as an attack-slot replacement, RTF1 back-pressure, a failed pass that never clicks the lie dialog, and a finite balance that wins over the unlimited marker; normal and standalone EXE packages rebuilt from this commit. |
| `1.2.105` – `1.2.111` | — | Intermediate builds of the same work. |
| `1.2.104` | `release/v1.2.104` | Reload-proven channel switch, 被撞反击, 多等 in seconds, post-switch map re-anchor. |
| `1.2.103` | — | The lie credential survives a completed pass. |
| `1.2.96` | `release/v1.2.96` | Version-bump checkpoint commit only. |
| `1.2.95` | `release/v1.2.95` | Synchronized auto-lie usage and device status. |
| `1.1.65` | `release/v1.1.65` | Climb-aware jump-point triggering and compact arrows. |
| `1.1.69` | `release/v1.1.69` | First protected EXE package; corrected Tk runtime packaging. |
| `1.1.64` | `release/v1.1.64` | 64-bit native marker-canvas handle repair. |
| `1.1.63` | `release/v1.1.63` | Persistent patrol-marker shared-canvas repair. |
| `1.1.62` | `release/v1.1.62` | Atomic patrol-marker canvas release. |
| `1.1.61` | `release/v1.1.61` | Longer, reliable patrol-point marker overlay release. |
| `1.1.60` | `release/v1.1.60` | Directional patrol-point marker release. |
| `1.1.59` | `release/v1.1.59` | Patrol-attack naming and marker-overlay release. |
| `1.1.58` | `release/v1.1.58` | Directional jump-point rope-hold release. |
| `1.1.57` | `release/v1.1.57` | Jump-point rope-input timing release. |
| `1.1.56` | `release/v1.1.56` | Jump-point capture-grid tolerance release. |
| `1.1.55` | `release/v1.1.55` | Jump-point dispatch reliability release. |
| `1.1.54` | `release/v1.1.54` | Routine-log cleanup release. |
| `1.1.53` | `release/v1.1.53` | First durable release checkpoint. |

Releases before `1.1.53` were distributed as replace-in-place ZIPs without
matching Git checkpoints, so their exact historical source cannot be recovered
reliably from a version number alone.

### Which SSH key pushes which remote

Two account keys live in `~/.ssh`, and a push must select the **matching** key
instead of relying on a default identity:

| Key | Account | State |
| --- | --- | --- |
| `~/.ssh/forbestop22_key` | `forbestop22` | Accepted by GitHub; this is the working push key. |
| `~/.ssh/lonkudo_github_ed25519` | `lonkudo` | Not registered with GitHub — it is refused with `Permission denied (publickey)`. Re-add the matching `.pub` file to that account before relying on it. |

Each private key sits beside its own `.pub` file, so the public half can be
re-registered without regenerating the pair.

```powershell
$key = ($env:USERPROFILE -replace '\\','/') + "/.ssh/forbestop22_key"
$env:GIT_SSH_COMMAND = "ssh -i `"$key`" -o IdentitiesOnly=yes"
git push git@github.com:forbestop22/maple-assistant.git main
```

The key path must use forward slashes. A Windows-style path is passed through
`GIT_SSH_COMMAND` with its backslashes stripped (`C:UsersSOTTES.ssh...`), and
the push then fails with `Permission denied (publickey)` even though the key is
present.

`forbestop22` has write access to the `forbestop22` mirrors but not to
`lonkudo/maple-assistant`, which answers
`ERROR: Permission to lonkudo/maple-assistant.git denied to forbestop22`. Push
to the mirror, or grant that account access to the canonical repository first.

See [ARCHITECTURE.md](ARCHITECTURE.md) for component ownership, data flow, and concurrency rules.
