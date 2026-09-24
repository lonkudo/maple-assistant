# Maple Assistant

Maple Assistant is a Windows desktop companion for recorded minimap patrols. It combines a compact Chinese interface, shared screen capture, map-aware movement, configurable attacks and consumables, alerts, recovery tools, and optional quick-message/trade helpers.

The assistant is designed around one principle: **automation must be observable, interruptible, and safe to stop**. Patrol input is disarmed until a patrol is explicitly started, and every worker uses the same foreground-window safeguards.

## Install and start

1. Extract the newest `MapleAssistant-<version>.zip` to a normal writable folder.
2. Run `安装.bat` once. Approve the single Windows permission prompt when requested.
3. Run `启动助手.bat`.

The release contains one package only. There is no separate CPU/CUDA edition: automatic lie handling uses the configured remote API rather than a local model runtime.

The update icon in the title bar searches the Desktop, the running folder, and its parent folder for a newer release package. A successful update replaces program files, imports a newer user configuration only when its configuration revision is newer, removes the consumed ZIP, and restarts the assistant.

## Configuration

Two configuration files keep personal settings separate from shipped defaults:

| File | Purpose |
| --- | --- |
| `user_config.json` | Your keys, layers, patrol route, alerts, quick messages, and personal settings. |
| `system_config.json` | Application defaults and internal behavior settings. |

Use **导入配置** and **导出配置** in the running-log panel to move your personal configuration between installations. Import and export actions are recorded in the log.

## Interface layout

The interface calculates each main column from its widest supported content when it is created, then keeps that column width fixed for the rest of the session. Switching attack modes only enables or disables the controls already reserved in the layout; it does not repack rows or resize either column. The window height remains adjustable and is allowed to grow or shrink as content such as quick-message rows changes.

## Recording a map

Record each map before enabling a route:

1. Record the **left endpoint** of the lowest layer.
2. Record its rope point when the layer has a rope.
3. Record the **right endpoint**.
4. Add the layer above it and repeat.
5. Select the patrol start and end layers.

Layers are stored from bottom to top. A patrol route can cover any contiguous range; a map does not need exactly three layers.

The minimap detector first finds the actual map border and calculates marker coordinates relative to that border. A broad search rectangle may help locate a map, but it is never saved as map geometry. This matters when the minimap size changes between maps or when the UI temporarily covers part of the game window.

At patrol start, the assistant focuses the game, uses the recorded minimap geometry, identifies the current layer, and either starts that layer’s route or begins the recorded return path. The selected layer is visible in the UI, and optional minimap overlays can show the recorded layer bands for inspection.

## Patrol, movement, and combat

### Patrol safety

- Movement is sent only while the selected game window is safely foregrounded.
- Left, right, up, and down are serialized. Direction switches release the old direction before arming the new one.
- Patrol, rope return, recovery, small-step, buffs, and special motions use coordinated input ownership so a stale key cannot keep the character moving.
- If patrol has no recorded route, the assistant remains in a safe stand-still state and combat functions can still operate.

### Attack modes

**固定攻击** performs the configured attack key at its selected fixed interval. **小碎步** and **重置空打** are optional timed motions, each with its own interval.

**小碎步** is an atomic left/right correction. Each direction is held for 220 ms with a 100 ms neutral gap; normal patrol resumes only after the sequence completes. **重置空打** performs a direction-preserving jump sequence with its own independent interval.

**站桩攻击** captures the character’s current location each time patrol starts. It is independent of recorded map layers and uses that temporary position only for its own local recovery. **跳打**, **小碎步**, and **朝向** are available in this mode. 朝向 can be left, right, or 双向; 双向 flips the required facing every 80 settled minimap frames through a short atomic facing tap. Patrol start itself does not issue a facing tap.

HP and MP potions have priority over ordinary combat actions. Pet food is treated as a timed consumable and does not reserve movement input.

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

The in-app running log is the first place to check. Critical unexpected errors are also written to `error.log`.

For a failed patrol start, check in this order:

1. The correct game window can be focused.
2. The minimap border and yellow character marker are visible.
3. The relevant layer endpoints and rope point have been recorded.
4. The patrol start/end layer selection is valid.

For a minimap-less area, return to a normal map before expecting patrol or disconnect monitoring to resume.

## Development and releases

Behavior changes are released as a new numbered ZIP:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1 -SkipTests
```

The release script advances `VERSION`, creates `release/MapleAssistant-<version>.zip`, and removes the previous release ZIP. Field testing is the normal validation path; do not run broad unit-test suites unless specifically requested.

### Release-to-Git mapping

Each shipped version is committed and marked with a Git tag in the form
`release/vX.Y.Z`. The tag is the authoritative mapping from a user-reported
version to its exact source snapshot; use `git show release/vX.Y.Z` when
investigating an older build. Tags are created after the release ZIP is built,
and must not be moved after publication.

| Release version | Git tag | Status |
| --- | --- | --- |
| `1.1.57` | `release/v1.1.57` | Jump-point rope-input timing release. |
| `1.1.56` | `release/v1.1.56` | Jump-point capture-grid tolerance release. |
| `1.1.55` | `release/v1.1.55` | Jump-point dispatch reliability release. |
| `1.1.54` | `release/v1.1.54` | Routine-log cleanup release. |
| `1.1.53` | `release/v1.1.53` | First durable release checkpoint. |

Releases before `1.1.53` were distributed as replace-in-place ZIPs without
matching Git checkpoints, so their exact historical source cannot be recovered
reliably from a version number alone.

See [ARCHITECTURE.md](ARCHITECTURE.md) for component ownership, data flow, and concurrency rules.
