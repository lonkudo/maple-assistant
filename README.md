# Maple Assistant

Independent workers analyze the game without relying on slow interactive computer control:

1. `capture_worker.py` captures the small minimap and status regions every four seconds.
2. `movement_worker.py` locates the yellow minimap diamond and recommends or performs movement.
3. `status_worker.py` monitors HP/MP and handles potions.
4. `attack_worker.py` sends Ctrl on its own timer, independently of movement.
5. `ui_worker.py` displays read-only debug information without blocking automation.

`minimap_detector.py` uses OpenCV contour detection to locate the resizable
top-left minimap. Movement consumes the dynamically detected analysis region;
the old normalized rectangle is used only as a low-confidence fallback. The UI
shows the detected minimap size, boxes, confidence, minimap preview, and cropped
map-name area. Map-name OCR is a replaceable adapter because OpenCV locates text
regions but does not itself recognize Chinese text.

The UI also owns manual multi-layer patrol controls. Move the character by hand
and wait until the displayed yellow-diamond position updates, then record
`Left-most`, `Rope`, and `Right-most`. Recorded buttons lock automatically and
turn grey; click the same embedded button once to unlock it, then click it again
to record the new position. Each point is saved to six decimal places in the
shared `recording-configuration.json`. After all three are present, `Add Layer Above`
creates/selects the next layer and pauses patrol while it is calibrated. A new
layer must have a smaller minimap Y than the layer below and becomes active only
after all three of its points are recorded. `Start Patrol` then runs each layer's
left/right route, uses that layer's own rope to climb, and changes to `Stop
Patrol` while active. These controls never send keys from the UI thread—the
movement worker remains the only movement executor.

UI-recorded points use adaptive minimap coordinates. OpenCV detects the inner
map canvas, and the marker detector measures the full anti-aliased yellow
diamond. Positions are stored as diamond-sized offsets from the canvas center,
then projected into the current canvas at runtime. Small animation-related
diamond-size changes are median-smoothed; a large size jump is treated as a
real zoom change immediately. Horizontal tolerance, near-rope range, final
movement zone, and layer-Y tolerance scale with the current diamond size.
Layers shown as `legacy layout` should be re-recorded once in the UI to gain
adaptive width/zoom mapping; `adaptive` layers already include this metadata.

The assistant starts in **live mode** and presses keys using Python `SendInput`.

```powershell
python assistant.py --debug-dir work/debug
```

Disable only the debug UI when desired:

```powershell
python assistant.py --no-ui --debug-dir work/debug
```

To analyze without pressing keys, explicitly request dry-run mode:

```powershell
python assistant.py --dry-run --debug-dir work/debug
```

The live assistant intentionally refuses to send keys unless the configured
game window is currently in the foreground. Losing focus pauses movement,
attacks, potion analysis, and releases held keys without closing the debug UI;
manually selecting the game again resumes automation. The assistant never
steals focus to resume itself. Potion readings also require a
confidence of at least 0.55 and two consecutive low frames, preventing an
uncertain color match from consuming an item.

All live gameplay actions are generated inside Python through Win32 `SendInput`
scan-code keyboard events. The assistant does not use Computer Use, mouse
automation, UI Automation, or terminal-driven key actions.
At live startup the Python sender finds exactly one matching MapleStory window,
restores it when minimized, brings it to the foreground, and stores its HWND.
Movement and attack are independent workers. Attack runs every three seconds
throughout navigation and sends only Ctrl; it never presses or releases Left or
Right. Therefore it cannot cancel the movement worker's direction hold.
Ctrl and Alt+Up use a shared critical-section lock, so an attack cannot
interrupt climbing. While the marker remains aligned, Alt+Up retries on each
new screenshot instead of stopping permanently after one attempt.
When minimap X distance is 0.04 or less, Ctrl attacks pause completely so the
final Left/Right correction and Alt+Up cannot be interrupted. HP/MP monitoring
and potion use remain active.
Movement uses fixed 2-second holds whenever the rope distance is greater than
0.04 normalized minimap units. Only at distance 0.04 or less is the final hold
calculated. Alignment uses a narrow 0.010 normalized-X tolerance—about two
pixels in the calibrated minimap crop—confirmed twice before Alt + Up.
Left and Right use adaptive holds based only on minimap X distance: 2 seconds
when far away, 0.7 seconds at medium range, and 0.18-second corrections near
the rope. Each hold emits a matching key-up before the next position decision.
Live logs include `key-down`, requested duration, `key-up`, and actual duration
so the two-second hold can be verified directly.
Climbing uses a narrow 0.008 minimap-X tolerance and requires three consecutive
aligned screenshots before Alt + Up.

`rope_calibration.json` stores reusable movement/climb timing. The shared
`recording-configuration.json` stores the recorded layers, endpoints, ropes,
and explicit route order instead of creating one configuration file per map.
Add layer 2 under its
`layers` object and append `layer2` to `route_order` after calibration.
while the character was aligned with the rope. On every startup the movement
worker detects the current yellow diamond, moves Left/Right toward that saved
X, then taps Alt and holds Up for 0.45 seconds to grab and climb the rope.

Stop safely with `Ctrl+C`. Keep the game window visible and use the default 2560x1600 client layout during initial calibration. The analyzers use normalized regions, so other 16:10 resolutions should also work.

Important: automation may violate a game server's rules. Use only where permitted.
