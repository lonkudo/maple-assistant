# Patrol + YOLO Detection Architecture Overview

Two processes, three state files (flag control). This document explains who
runs what and how they coordinate.

> **Note on synchronization:** coordination is done with **flag/state files**
> (advisory handshakes), not a mutex.  A named-mutex experiment
> (`input_mutex.py`, commit a0b36cc) was reverted (c8d6e7a) because it
> depended on process start order and could freeze movement.  The state
> files are order-independent: whoever reads treats a missing/stale file as
> "not busy".

## 1. The two processes

```
┌─────────────────────────────────────────────────────────────────────┐
│ PROCESS A: assistant.py  (Python 3.10 env)                          │
│   "the patrol process"                                              │
│                                                                     │
│   Workers (threads):                                                │
│   ├─ CaptureWorker   ── grabs screen frames, publishes to a bus     │
│   ├─ MovementWorker  ── PATROL: minimap-based route movement,       │
│   │                     rope approach, climbing, dropping           │
│   ├─ StatusWorker    ── HP/MP bar reading (passive)                 │
│   ├─ PickupWorker    ── taps Z while walking                        │
│   ├─ AttackWorker    ── legacy timed Ctrl (disabled by default)     │
│   └─ FocusWorker     ── keeps the game window focused               │
│                                                                     │
│   Owns: attack/patrol/rope state files (read/write)                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ PROCESS B: yolo-detection/live_view.py  (venv313 - has torch)       │
│   "the attack process" (launched by the UI as a subprocess)         │
│                                                                     │
│   Loop (10 fps default, adjustable):                                │
│   ├─ capture screen (mss)                                           │
│   ├─ YOLO detect: mobs, character, environment (preview), rope      │
│   ├─ attack_decision → best mob in range                            │
│   ├─ AttackExecutor.attack() → face + press Ctrl                    │
│   └─ publish state files for the patrol process                     │
└─────────────────────────────────────────────────────────────────────┘
```

Both processes inject keys into the SAME game window.  State files keep
them from fighting over the keyboard; each worker reads the other's flag
before acting.

## 2. The three state files (flag control)

All in `work/`, written atomically (temp file + rename).  Freshness is
checked with timestamps so a dead process can't wedge the other one.
A missing or stale file always reads as "not busy" - start order does not
matter (patrol first, detect first, or either alone).

| File | Writer | Reader | Meaning |
|---|---|---|---|
| `attack_state.json` | live_view (B) | MovementWorker (A) | "I have a mob target" → patrol pauses movement (attack priority) |
| `patrol_state.json` | MovementWorker (A) | AttackExecutor (B) | "I'm climbing/dropping" → attack blocks |
| `rope_state.json` | live_view (B) | MovementWorker (A) | rope screen X + character screen X → YOLO-gated rope jump |

Priority: **who WANTS control** is decided by the state files.
- If B has a target → A holds still (fight).
- If A is climbing → B holds fire (climb).
- Otherwise A walks its patrol route.

Because the files are timestamped, transient races are tolerated: patrol
checks attack_state before each frame's movement, attack checks
patrol_state before each Ctrl.  The two workers cannot run forever in
conflict - the next frame re-reads the flags.

## 3. Patrol logic detail (MovementWorker, process A)

Input: minimap frames. Route = calibrated layers from
`recording-configuration.json` (left_most_pos / right_most_pos / rope_pos /
layer_y per layer, route_order, first_layer, final_layer_action).

Per frame:
1. Detect minimap → player marker position, layer detection (by Y / world-Y).
2. Attack-priority gate: if `attack_state.json` is active → skip movement.
3. `_resync_route_layer`: if the marker is on another layer (fall, climb
   success), switch route state.
4. `_route_target`: current endpoint (left-most / right-most / rope /
   drop-to-first) for the current layer + phase.
5. Movement decision:
   - walking to an endpoint → `left` / `right` tap (calculated hold)
   - final layer done → drop through platforms down to first layer
     (`_descending_to_first` flag suppresses resync hijack until layer1)
   - **rope approach**: YOLO asked FIRST via `_yolo_rope_action()`:
     - |gap| <= on_rope_px  → no-op decision → patrol's climb state holds Up
     - on_rope < |gap| <= jump_px → `jump_climb_left/right` (screen-based)
     - gap too large / stale → minimap `move_towards_rope` walk plan
   - climb: `climb()` runs the directional jump chord, then `persistent_up`
     holds Up; `preserve_persistent_climb` keeps Up held while climbing;
     stall detection releases Up if world-Y stops advancing.
6. Publish `patrol_state.json` (busy = climbing/dropping).
7. Send the movement key through the shared WindowKeySender (foreground
   checked; dry-run safe).

## 4. YOLO detection logic detail (live_view.py, process B)

Per frame (all wrapped in try/except - a bad capture/model frame is logged
and skipped, the process no longer dies):

1. Capture screen (mss), 10 fps default.
2. `bot.detect_objects(img)` → mobs only, center-zone filtered, min-box
   filter (tiny boxes = misclassified drops are ignored).
3. `bot.detect_character(img)` → your character (center-dominant scoring,
   median smoothing).
4. `bot.attack_decision(mobs, character, attack_range)` → nearest mob
   within the drawn range line (attack_range/2 in x and y; min box size).
5. Preview overlay: character/environment/mob boxes with per-class colors,
   zone-restricted.
6. If attack enabled and target chosen → `executor.attack()` (face + Ctrl,
   blocked while patrol_state says climbing/dropping).
7. Publish `attack_state.json` (target present?) for patrol.
8. `detect_rope(img)` → tall/narrow environment box nearest the character;
   publish `rope_state.json` (rope X + character X) for the YOLO-gated jump.

The model itself (weights/best.pt, 6 classes: character/environment/item/
mob/npc/ui) never patrols - patrol is 100% minimap logic; YOLO only adds
mob/character/rope sensing on the main screen.

## 5. Coordination flow for one full patrol loop

```
patrol walks layer1 left→right → reaches rope zone
  → YOLO sees rope: gap in range → jump_climb (screen direction)
  → character on rope (gap <= on_rope) → YOLO silent, patrol holds Up
  → climbs to layer2 (patrol_state busy → attack blocked)
  → layer2 patrol, etc. → final layer → drop down to layer1 → repeat
any mob in range while walking:
  → attack_state active → patrol pauses movement
  → executor faces, presses Ctrl, clears target
  → target gone → patrol resumes
```

## 6. Files

| File | Role |
|---|---|
| `assistant.py` | patrol process entry; wires workers + state paths |
| `movement_worker.py` | patrol logic (route, rope, climb, drop, flags) |
| `yolo-detection/live_view.py` | attack process entry; detection loop |
| `yolo-detection/attack_executor.py` | face+Ctrl executor (patrol-state gate) |
| `yolo-detection/auto.py` | YOLO wrapper: detect_objects/character/rope, attack_decision, ConfigManager |
| `combat_coordination.py` | AttackStateFile / PatrolStateFile / RopeStateFile |
| `status_worker.py` | WindowKeySender (scan-code key injection) |
| `pickup_worker.py` | Z taps while walking (not YOLO-based) |

## 7. Golden rules (do not break)

1. State files are advisory handshakes; a stale/missing file must behave
   like "not busy" so neither process wedges and start order never matters.
2. Patrol is minimap-only; YOLO is screen-only.  YOLO's rope role is
   limited to the jump (direction + timing); walking and climbing stay in
   patrol.
3. The preview shows only character/environment/mob, inside the zone.
4. Never reintroduce a blocking lock across processes without handling
   start order, abandoned ownership, and crash cleanup - the mutex
   experiment failed on exactly these.
