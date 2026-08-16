# Patrol + YOLO Detection Architecture Overview

Two processes, three state files, one named mutex. This document explains who
runs what, how they coordinate, and where the mutex sits.

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
│   Owns: input_mutex (shared handle), attack/patrol/rope state files │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ PROCESS B: yolo-detection/live_view.py  (venv313 - has torch)       │
│   "the attack process" (launched by the UI as a subprocess)         │
│                                                                     │
│   Loop (10 fps default, adjustable):                                │
│   ├─ capture screen (mss)                                           │
│   ├─ YOLO detect: mobs, character, environment (preview), rope      │
│   ├─ attack_decision → best mob in range                            │
│   ├─ AttackExecutor.attack() → face + press Ctrl (under mutex)      │
│   └─ publish state files for the patrol process                     │
└─────────────────────────────────────────────────────────────────────┘
```

Both processes inject keys into the SAME game window. That is the whole
synchronization problem: they must never press keys at the same moment.

## 2. The three state files (handshakes, not locks)

All in `work/`, written atomically (temp file + rename). Freshness is
checked with timestamps so a dead process can't wedge the other one.

| File | Writer | Reader | Meaning |
|---|---|---|---|
| `attack_state.json` | live_view (process B) | MovementWorker (A) | "I have a mob target" → patrol pauses movement (attack priority) |
| `patrol_state.json` | MovementWorker (A) | AttackExecutor (B) | "I'm climbing/dropping" → attack blocks |
| `rope_state.json` | live_view (B) | MovementWorker (A) | rope screen X + character screen X → YOLO-gated rope jump |

Priority decision: **who WANTS control** is decided by the state files.
- If B has a target → A holds still (fight).
- If A is climbing → B holds fire (climb).
- Otherwise A walks its patrol route.

## 3. The mutex: who OWNS the keyboard

State files are advice; the mutex is the law. `input_mutex.py` wraps a
Windows **named mutex** (`Local\MapleAssistant.InputControl.v1`) — named
mutexes are visible across processes, which is why this works even though
the two processes can't share a `threading.Lock`.

### Patrol side (MovementWorker, process A)

```
movement send block:
    if not self._acquire_input():      # try_acquire(200ms)
        continue                        # attack owns keyboard → skip frame
    try:
        send drop / climb / left / right tap
    finally:
        if not self._action_spans_frames(decision):
            self._release_input()       # plain taps release immediately
```

- `_acquire_input()`: `try_acquire(200ms)`; keeps `_input_held` flag.
- **Climb / jump_climb / drop keep the mutex held across frames** (the
  persistent Up hold / drop chord lasts many frames — the mutex must stay
  with patrol for the whole action).
- Plain left/right taps: acquire → send → release (one frame).
- On worker stop: `_release_input()` so the attack process is never
  starved by a dead patrol.

### Attack side (AttackExecutor, process B)

```
attack():
    ...
    if self._input_mutex is not None:
        if not self._input_mutex.try_acquire(0):   # zero timeout
            return False                            # patrol owns keyboard
    try:
        face tap (if needed) + settle sleep + Ctrl tap
    finally:
        self._input_mutex.release()
```

- Attack asks with **0 ms timeout**: if patrol holds the mutex (climbing),
  the attack is skipped entirely — it never waits and never queues.
- Acquires only for the duration of one attack (turn + settle + Ctrl),
  then releases immediately, so patrol can resume next frame.

### Mutex position summary

```
patrol walk tap:   acquire → send → release           (per frame)
patrol climb/drop: acquire → HOLD → release when done (spans frames)
attack tap:        acquire(0) → face+ctrl → release   (per attack, never waits)
```

Because climbing holds the mutex, and attack uses timeout 0, **attack
physically cannot press Ctrl while patrol is climbing** — this is the hard
guarantee the `patrol_state.json` handshake only approximated.

## 4. Patrol logic detail (MovementWorker, process A)

Input: minimap frames. Route = calibrated layers from
`recording-configuration.json` (left_most_pos / right_most_pos / rope_pos /
layer_y per layer, route_order, first_layer, final_layer_action).

Per frame:
1. Detect minimap → player marker position, layer detection (by Y / world-Y).
2. `_resync_route_layer`: if the marker is on another layer (fall, climb
   success), switch route state.
3. `_route_target`: current endpoint (left-most / right-most / rope /
   drop-to-first) for the current layer + phase.
4. Movement decision:
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
5. Attack-priority gate: if `attack_state.json` is active → skip movement.
6. Publish `patrol_state.json` (busy = climbing/dropping).
7. Send keys under the input mutex (section 3).

## 5. YOLO detection logic detail (live_view.py, process B)

Per frame (all wrapped in try/except — a bad capture/model frame is logged
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
   under the mutex, blocked while patrol climbs).
7. Publish `attack_state.json` (target present?) for patrol.
8. `detect_rope(img)` → tall/narrow environment box nearest the character;
   publish `rope_state.json` (rope X + character X) for the YOLO-gated jump.

The model itself (weights/best.pt, 6 classes: character/environment/item/
mob/npc/ui) never patrols — patrol is 100% minimap logic; YOLO only adds
mob/character/rope sensing on the main screen.

## 6. Coordination flow for one full patrol loop

```
patrol walks layer1 left→right → reaches rope zone
  → YOLO sees rope: gap in range → jump_climb (screen direction)
  → character on rope (gap <= on_rope) → YOLO silent, patrol holds Up
  → climbs to layer2 (patrol_state busy → attack blocked via mutex)
  → layer2 patrol, etc. → final layer → drop down to layer1 → repeat
any mob in range while walking:
  → attack_state active → patrol pauses movement
  → executor takes mutex, faces, presses Ctrl, releases
  → target gone → patrol resumes
```

## 7. Files

| File | Role |
|---|---|
| `assistant.py` | patrol process entry; wires workers + mutex + state paths |
| `movement_worker.py` | patrol logic (route, rope, climb, drop, mutex) |
| `yolo-detection/live_view.py` | attack process entry; detection loop |
| `yolo-detection/attack_executor.py` | face+Ctrl executor (mutex, patrol gate) |
| `yolo-detection/auto.py` | YOLO wrapper: detect_objects/character/rope, attack_decision, ConfigManager |
| `combat_coordination.py` | AttackStateFile / PatrolStateFile / RopeStateFile |
| `input_mutex.py` | cross-process named mutex |
| `status_worker.py` | WindowKeySender (scan-code key injection) |
| `pickup_worker.py` | Z taps while walking (not YOLO-based) |

## 8. Golden rules (do not break)

1. The mutex is the ONLY thing that prevents simultaneous key presses.
   Never send keys outside `_acquire_input`/mutex scope.
2. Climb/drop must HOLD the mutex for their whole duration; taps release
   immediately.
3. Attack uses timeout 0 — it never queues behind patrol; it either owns
   the keyboard now or skips.
4. State files are advisory; a stale file (dead process) must behave like
   "not busy" so nothing wedges.
5. Patrol is minimap-only; YOLO is screen-only. YOLO's rope role is
   limited to the jump (direction + timing); walking and climbing stay in
   patrol.
6. The preview shows only character/environment/mob, inside the zone.
