import sys
import io
import ctypes
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\SOTTES\Documents\Codex\2026-08-12\skill-creator-c-users-sottes-codex-2\yolo-detection")
import numpy as np
import mss
from auto import OptimizedMapleBot

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

bot = OptimizedMapleBot()
bot.model.conf = 0.3
region = bot.monitor

with mss.MSS() as sct:
    for i in range(5):
        shot = sct.grab(region)
        img = np.ascontiguousarray(np.array(shot)[:, :, :3])
        mobs = bot.detect_objects(img)
        character = bot.detect_character(img)
        target = bot.attack_decision(mobs, character, attack_range=800)
        line = f"t={i}: mobs={len(mobs)}"
        if character:
            line += (
                f" player={character.confidence:.2f}@({character.center[0]},"
                f"{character.center[1]})"
            )
        else:
            line += " player=NONE"
        line += f" target={'YES' if target else 'NO'}"
        if target and character:
            dx = target.center[0] - character.center[0]
            dy = target.center[1] - character.center[1]
            line += f" (dx={dx:+d},dy={dy:+d})"
        print(line)
        time.sleep(2)
