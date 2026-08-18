# Maple Assistant — Installation & Resolution Guide

## 1. Install (for the end user)

The project is self-bootstrapping. No Python knowledge is needed.

1. Unzip the release folder.
2. Right-click **`install.ps1`** → *Run with PowerShell* (or run
   `powershell -ExecutionPolicy Bypass -File install.ps1`).
   - The script finds a Python 3.10–3.12 on your machine (your environment);
     if none exists it **downloads and installs Python 3.12 automatically**
     (winget first, python.org fallback).
   - It creates a local `.venv` and installs the requirements
     (numpy, Pillow, OpenCV, pywin32).
   - It creates two launchers: **`start_assistant.bat`** and **`启动助手.bat`**.
3. Optional mob detection (YOLO, downloads several GB of PyTorch):
   `powershell -ExecutionPolicy Bypass -File install.ps1 -Yolo`
   - Creates `yolo-detection\venv313` (the exact path the UI expects) and
     installs torch/ultralytics.
   - Put your trained model at `yolo-detection\weights\best.pt`.
4. Double-click **`启动助手.bat`** to start. The debug UI opens; click
   **Start Patrol** when the game window is in the foreground.

> The assistant works **without** the YOLO part — use the Fixed Attack panel
> for plain farming. `-Yolo` is only for the AI mob detection.

## 2. Supported screen resolutions

Everything is **normalized to the game client**, so it adapts to the
resolution you play at:

| Resolution | Aspect | Status |
|---|---|---|
| 2560×1440 | 16:9 | ✓ works |
| 1920×1080 | 16:9 | ✓ works |
| 1366×768 | 16:9 | ✓ works |
| 2560×1600 | 16:10 | ✓ works (the original calibration) |

How it works:

- The capture now grabs the **full client window**, and every analysis region
  (minimap, HP/MP bars) is a normalized fraction of the client. The minimap is
  located dynamically with OpenCV contours, patrol points are stored as
  diamond-relative offsets, and the HP/MP bar widths are fractions of the
  client width — so none of them care about your resolution.
- The YOLO detector is configured with a **pixel** capture region
  (`yolo-detection\config.yaml` → `window.default`) and a pixel `--attack-range`.
  Set these once for your resolution via the YOLO panel (they are saved to
  `yolo_detection_settings.json` / `config.yaml`).

Things to do **once per resolution** (switching resolutions changes the UI
scale slightly):

1. Verify the minimap is detected (the UI shows the minimap preview + boxes).
2. Verify the HP/MP numbers in the status row look right; if they read ~10%
   off, adjust the bar-width fraction in `status_worker.py`
   (`full_bar_width_fraction`, measured as full-bar-pixels ÷ client-width).
3. If you re-record patrol points, record them **at the resolution you will
   play at** — the diamond-relative storage makes them portable across
   resolutions, but recording fresh at the target resolution is the most
   accurate.

## 3. Troubleshooting

- **"No module named X"** — the venv is incomplete: re-run `install.ps1`.
- **Game window selection fails on Start Patrol** — the assistant never sends
  Alt (Alt is the jump key), so focus grabbing relies on direct
  `SetForegroundWindow`; click Start Patrol while the game window is visible.
- **Character jumps when patrol starts** — this was fixed (no Alt during
  window selection + a start grace period); if you still see it, check the
  `patrol_start_grace_seconds` / `alt_transition` settings.
- **YOLO window says venv missing** — run `install.ps1 -Yolo`.

## 4. Safety

Automation may violate a game server's rules. Use only where permitted.
