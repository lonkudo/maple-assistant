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
shared `recording-configuration.json`. `Add Layer Above`
creates/selects the next layer and pauses patrol while it is calibrated. A new
layer must have a smaller minimap Y than the layer below.

**Left, Rope, and Right are independent actions.** A layer patrols exactly the
points you record, in left → right → rope order: a layer with only `Rope`
goes straight to its rope and climbs; a layer with only `Left` stands at the
left-most point; a layer with `Left`+`Right` patrols that floor back and forth
(no climb); and a layer (or whole map) with nothing recorded stands still and
only attacks (useful with Fixed Attack or YOLO farming). The final layer's rope
is omitted (there is nothing above it to climb to). `Start Patrol` then runs each
layer's recorded actions, uses that layer's own rope to climb, and changes to `Stop
Patrol` while active. `Start Patrol` is enabled as soon as every layer has at least
one recorded point (or nothing is recorded at all, for stand-still + attack) - it
does not require re-recording or an unlock step. These controls never send keys from
the UI thread—the movement worker remains the only movement executor.

UI-recorded points use adaptive minimap coordinates. OpenCV detects the inner
map canvas, and the marker detector measures the full anti-aliased yellow
diamond. Positions are stored as diamond-sized offsets from the canvas center,
then projected into the current canvas at runtime. Small animation-related
diamond-size changes are median-smoothed; a large size jump is treated as a
real zoom change immediately. Horizontal tolerance, near-rope range, final
movement zone, and layer-Y tolerance scale with the current diamond size.
Layers shown as `legacy layout` should be re-recorded once in the UI to gain
adaptive width/zoom mapping; `adaptive` layers already include this metadata.
Legacy layers still start patrol (their recorded ratios are used as-is), just
without zoom adaptation.

When enabled, the other-player safety net scans the minimap for red diamonds
(other players) and auto-switches channel on sighting. The scan is time-anchored
(every `other_player_check_interval_seconds`, default 60 s) instead of on every
patrol cycle, so it costs almost no extra CPU/GPU.

**Stair jumps (automatic).** Stairs that block the left/right patrol walk (the
character bumps into them and the minimap X stops advancing) are jumped
automatically — no jump points need to be recorded. During the
move-to-left-most/right-most phases, when the marker stalls for
`stair_jump_stall_frames` (default 2) no-progress frames while a walk hold is
being issued, the worker jumps: it holds the travel direction and taps Alt
mid-hold to carry over the stair. Jumps are grace-limited
(`stair_jump_grace_seconds`, default 2.5 s) and capped
(`stair_jump_attempts_max`, default 3), after which the bot logs
`STAIR JUMP gave up ...` and keeps walking so a truly impassable wall cannot
make the character hop in place forever. Tuning knobs
(`stair_jump_stall_diamonds`, `stair_jump_alt_hold_seconds`,
`stair_jump_lead_seconds`) live in `rope_calibration.json`.

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
steals focus to resume itself. Potions are the highest-priority action: they
need two consecutive low frames, and a low-confidence read only suppresses a
potion when NO bar is below its threshold (a near-empty bar reads as a tiny
fill run with low confidence - exactly when the potion is needed). A blocked
potion tap is retried (`potion_retry_attempts`, default 3) before the next low
frame tries again.

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
calculated. Rope approach has three gap zones (configured in
`recording-configuration.json` under `rope`): right on the rope
(|gap| ≤ `under_rope_tolerance`, 0.008) jumps straight up; the inner band
(|gap| ≤ `inner_range`, 0.018) jumps left/right toward the rope side; and the
honey zone (`inner_range` < |gap| ≤ `near_range`, 0.025) creeps with **tiny
random steps** (between `rope_tiny_step_min_seconds` /
`rope_tiny_step_max_seconds`, defaults 0.05 / 0.15 s) to adjust position —
outside the honey zone it uses the big walking holds. Alignment uses a narrow
0.010 normalized-X tolerance—about two pixels in the calibrated minimap
crop—confirmed twice before Alt + Up.
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
