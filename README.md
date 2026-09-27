# Maple Assistant

Maple Assistant is a Windows desktop companion for recorded minimap patrols. It combines a compact Chinese interface, shared screen capture, map-aware movement, configurable attacks and consumables, alerts, recovery tools, and optional quick-message/trade helpers.

The assistant is designed around one principle: **automation must be observable, interruptible, and safe to stop**. Patrol input is disarmed until a patrol is explicitly started, and every worker uses the same foreground-window safeguards.

## Install and start

Two packages are published for each version:

| Package | Audience | Start |
| --- | --- | --- |
| `MapleAssistant-<version>.zip` | normal package, needs a Python environment | extract, run `安装.bat` once, then `启动助手.bat` |
| `MapleAssistant-release-<version>.zip` | standalone package, no Python installation | extract, run `启动助手.bat` |

Normal package:

1. Extract the newest `MapleAssistant-<version>.zip` to a normal writable folder.
2. Run `安装.bat` once. Approve the single Windows permission prompt when requested.
3. Run `启动助手.bat`.

There is no separate CPU/CUDA edition: automatic lie handling uses the configured remote API rather than a local model runtime.

Standalone package:

1. Extract the whole `MapleAssistant-release-<version>.zip` folder — the executable needs the files beside it, so running `MapleAssistant.exe` alone is not supported.
2. Run `启动助手.bat` and approve the Windows permission prompt. The launcher is hidden and requests administrator rights, because the assistant must run at the same privilege level as the game or Windows silently drops injected keys.
3. If the assistant does not appear, run `诊断启动.bat` (`diagnose_start.bat`); it keeps its console window open and prints the startup error.

The standalone package starts no console window and needs no `安装.bat`: it carries its own Python runtime, the complete data layout of the normal package (configurations, assets, model weights), and the same `user_config.json` behavior. It ships no application source, so features that must start a *separate* Python interpreter cannot work there — the YOLO detector panel is the known case, and it stays hidden.

If a required library is missing, the normal package repairs itself: the hidden launcher writes the traceback to `assistant-launch-error.log`, runs `安装.bat` once automatically, and starts the assistant again in a fresh process. A failed automatic install leaves the log and a message box instead of a silent start.

The update icon in the title bar searches the Desktop, the running folder, and its parent folder for a newer release package. A successful update replaces program files, imports a newer user configuration only when its configuration revision is newer, removes the consumed ZIP, and restarts the assistant. Only packages that contain `assistant.py` are accepted as updates, so the standalone package never replaces an installation by itself.

## Configuration

Two configuration files keep personal settings separate from shipped defaults:

| File | Purpose |
| --- | --- |
| `user_config.json` | Your keys, layers, patrol route, alerts, quick messages, and personal settings. |
| `system_config.json` | Application defaults and internal behavior settings. |

Use **导入配置** and **导出配置** in the running-log panel to move your personal configuration between installations. Import and export actions are recorded in the log.

## Licensing and activation service

The desktop package verifies signed licenses locally using only an Ed25519
public key. The separate `maple-assistant-server` repository owns activation
records, device bindings, expiry calculation, and every server private key.
It must never be copied into a desktop release.

Server-issued activation codes are exactly 256 characters long (`MAL-` plus
252 cryptographically random hexadecimal characters). The server retains only
their SHA-256 hashes, binds one code to one device on first activation, and
starts a time-limited expiry then. A device may own multiple separately issued
codes; each individual code can bind to only one device. Ten failed activations
for one fingerprint within 24 hours ban that fingerprint.

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
The linked-node icon in **运行日志** copies this file to the clipboard.

The most recently submitted code and its validation result are stored in the
`license` section of `user_config.json`. The signed `license.json` document is
the actual local entitlement. A verified rejected replacement code immediately
locks automation and removes that signed document, so the rejected state also
persists after restart. A connection, TLS, or server-configuration failure is
reported separately and does not erase a previously saved entitlement.

Online auto-lie session/key delivery remains deliberately separate from the
existing auto-lie adapter until the versioned server-client session protocol is
completed.

## Interface layout

The interface calculates each main column from its widest supported content when it is created, then keeps that column width fixed for the rest of the session. Switching attack modes only enables or disables the controls already reserved in the layout; it does not repack rows or resize either column. The window height remains adjustable and is allowed to grow or shrink as content such as quick-message rows changes.

Every hint shown to the operator is Chinese, including the refusal reasons printed under **图层校准与巡逻** — a failed recording names the cause in Chinese (for example 最上层无法录制绳索点, 该楼层尚未录制, 未找到游戏窗口, 巡逻启动时未检测到黄色角色标记) instead of mixing an English reason into a Chinese prefix. Messages that only reach the log or a developer remain English on purpose, so a traceback can be searched for by its original wording.

## Recording a map

Record each map before enabling a route:

1. Record the **left endpoint** of the lowest layer.
2. Record its rope point when the layer has a rope.
3. Record the **right endpoint**.
4. Optionally record directional jump points with `Ctrl+D` (left) or `Ctrl+F` (right).
5. Add the layer above it and repeat.
6. Select the patrol start and end layers.

Layers are stored from bottom to top. A patrol route can cover any contiguous range; a map does not need exactly three layers.

Each layer's row has an axis band beside its name. **Right-click the axis** to open its point menu (添加最左 / 添加绳索 / 添加最右 / 添加左跳 / 添加右跳); the menu is only available while patrol is stopped, because recording is locked while it runs. A left click on empty axis space opens the same menu, while a left click on an already-recorded marker selects that layer instead — so the right click is the reliable way to the menu.

The minimap detector first finds the actual map border and calculates marker coordinates relative to that border. A broad search rectangle may help locate a map, but it is never saved as map geometry. This matters when the minimap size changes between maps or when the UI temporarily covers part of the game window.

At patrol start, the assistant focuses the game, uses the recorded minimap geometry, identifies the current layer, and either starts that layer’s route or begins the recorded return path. The selected layer is visible in the UI, and optional minimap overlays can show the recorded layer bands for inspection.

### Directional jump points

A jump point is a locked X/Y record on one layer. `Ctrl+D` records a **left jump** and `Ctrl+F` records a **right jump**; points are sorted by X and can be removed but not edited. During ordinary patrol, a point fires only while travelling in its recorded direction. During an active rope climb, either directional point can fire when the live marker matches it.

Matching keeps the horizontal window tight (±0.010 minimap units) and allows a wider vertical window (±0.020) so a rope approach can still meet the point. The resulting motion holds the recorded horizontal direction, presses jump, and holds Up until three later marker samples show that Y has settled on a horizontal platform.

At patrol start, all recorded points are displayed together for five seconds: blue bars for endpoints, a yellow up arrow for a rope, and thin green directional arrows for jump points. These overlays are diagnostic only and never participate in movement decisions.

## Patrol, movement, and combat

### Patrol safety

- Movement is sent only while the selected game window is safely foregrounded.
- Left, right, up, and down are serialized. Direction switches release the old direction before arming the new one.
- Patrol, rope return, recovery, small-step, buffs, and special motions use coordinated input ownership so a stale key cannot keep the character moving.
- If patrol has no recorded route, the assistant remains in a safe stand-still state and combat functions can still operate.

### Attack modes

**巡逻攻击** performs the configured attack key at its selected fixed interval. **小碎步** is an optional timed motion with its own interval.

**小碎步** is an atomic left/right correction. Each direction is held for 220 ms with a 100 ms neutral gap; normal patrol resumes only after the sequence completes.

**站桩攻击** captures the character’s current location each time patrol starts. It is independent of recorded map layers and uses that temporary position only for its own local recovery. **跳打**, **小碎步**, and **朝向** are available in this mode. 朝向 can be left, right, or 双向; 双向 flips the required facing every 80 settled minimap frames through a short atomic facing tap. Patrol start itself does not issue a facing tap.

HP and MP potions have priority over ordinary combat actions. Pet food is treated as a timed consumable and does not reserve movement input.

### Position correction while standing

站桩攻击 holds the recorded spot with one atomic motion per correction: a short direction hold — the tiny step inside the inner band, a longer walk when the character is further out — followed immediately by one attack. The attack belongs to the correction, so a correction can never be the moment a beat is lost; corrections repeat about 300 ms apart until the marker is back inside the arrival band (±0.006X). A drift is always walked back: the character never settles a pixel or two away from its 桩. The small-step band ends at ±0.010X, and the correction interval only spaces the corrections — it does not create a gap in which a beat is skipped.

Two related rules protect that motion:

- While the optional **小碎步** pair is queued or running, nothing is queued behind it. The pair deliberately steps away from and back to the selected 朝向 and sets the facing itself when it finishes, so a correction or facing tap queued during it used to land as an extra step (and an extra attack) immediately after 小碎步.
- The **朝向** tap is a 30 ms turn. It is long enough for the game to turn the character and short enough to stay inside the anchor band; a longer hold walks the character out of the band, and the correction that answers it turns the character back — the face/walk twitch.

### 捡东西 (stand-still pickup circuit)

**捡东西** walks the recorded left endpoint, then the right endpoint, then back to the 桩. Its trigger interval is set in **minutes** on its own row (`每 … m`, from 2.0m to 30.0m); the random gap beside it stays in seconds. That row is the only place minutes are used — the configuration file and the movement worker keep seconds, and the interval is clamped to the same 2–30 minutes when it is loaded.

## Alerts and recovery

The additional-functions panel can enable sound, screen flash, Telegram messages, disconnect alerts, lie-detection alerts, timer alarms, and other-player channel switching.

Disconnect handling deliberately avoids treating every missing minimap marker as a disconnect. At the shared 5 FPS capture rate, the marker must be absent for 50 samples (about ten seconds), **and** the current frame must show either the login-page evidence or the dedicated offline prompt. The prompt check remains valid when the prompt covers the login background. In minimap-less zones, monitoring pauses after the absence threshold and resumes when the marker returns; it does not repeatedly reconnect or stop patrol merely because a minimap is hidden.

Automatic reconnect begins only after this verified offline event and then follows the login-page workflow.

## Lie detection and automatic handling

The lie detector shares the normal 5 FPS capture cadence. When automatic lie handling is enabled:

1. Application startup performs a background, no-billing WebSocket endpoint probe and caches the usable endpoint.
2. On a lie event, the assistant opens a fresh authenticated connection while the game completes its visual transition.
3. The visual settle period is three seconds from event start.
4. Cursor-target frames are sent for up to thirteen seconds.
5. The round is ended with the API’s `round_end` message and the WebSocket is closed normally.

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
| `Ctrl+Z` | Toggle quick pickup while patrol is stopped. |

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

## Development and releases

Behavior changes are released as a new numbered ZIP:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

The release script advances `VERSION`, creates `release/MapleAssistant-<version>.zip`, and removes the previous release ZIP. Field testing is the normal validation path; do not run broad unit-test suites unless specifically requested.

Pass `-Version X.Y.Z` to publish an exact version without advancing it, and `-Minor` / `-Major` to move the second or first part.

The standalone package is built from that staged normal package afterwards, with the same version:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_protected_release.ps1 -Version X.Y.Z
```

Run the two commands in that order: `release_now.ps1` prunes `MapleAssistant-*.zip`, which also matches the standalone package name. The standalone build mirrors the staged package's data layout, installs any missing build requirement automatically, compiles from inside the staged package, refuses to publish an executable that cannot import a bundled module, and writes `release/MapleAssistant-release-<version>.zip`.

### Release-to-Git mapping

Each shipped version is committed and marked with a Git tag in the form
`release/vX.Y.Z`. The tag is the authoritative mapping from a user-reported
version to its exact source snapshot; use `git show release/vX.Y.Z` when
investigating an older build. Tags are created after the release ZIP is built,
and must not be moved after publication.

| Release version | Git tag | Status |
| --- | --- | --- |
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

See [ARCHITECTURE.md](ARCHITECTURE.md) for component ownership, data flow, and concurrency rules.
