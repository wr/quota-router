#!/usr/bin/env python3
"""Plays the README demo storyline in the terminal. Recorded with vhs:

    cd docs/demo && vhs demo.tape

Uses the real statusline renderer, so the GIF shows exactly what ships.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "router"))
import statusline as sl  # noqa: E402

CFG = {"statusline_style": "braille", "statusline_color": True,
       "weekly_protect_pct": 75, "fivehour_soft_pct": 85,
       "claude_cache_ttl_seconds": 90}
DIM, RESET = "\033[2m", "\033[0m"


def frame(caption, five, seven, codex, mins, hibernating, hold):
    now = time.time()
    cache = {"five_hour": {"used_percentage": five, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + mins * 60},
             "seven_day": {"used_percentage": seven, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + 4000 * 60}}
    sl._load_json_from_probe = lambda: {
        "available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": float(codex), "minutes_to_reset": 6000}}}
    # /dev/null exists, so the renderer sees a hibernation marker
    sl.HIBERNATE_MARKER = "/dev/null" if hibernating else os.path.join(HERE, "none")
    line = sl._render(cache, CFG, now)
    sys.stdout.write("\033[2J\033[H\n")
    sys.stdout.write(f"  {DIM}{caption}{RESET}\n\n")
    sys.stdout.write(f"  {line}\n")
    sys.stdout.flush()
    time.sleep(hold)


FRAMES = [
    ("Claude Code + Codex — both quota pools, one glance", 34, 12, 15, 41, False, 3.0),
    ("an afternoon of subagents later...", 62, 31, 16, 160, False, 2.5),
    ("the 5-hour window fills...", 81, 38, 18, 96, False, 2.5),
    ("...binding: new work routes to Codex instead", 93, 41, 27, 41, False, 3.5),
    ("capped mid-task -> hibernating until reset", 99, 42, 34, 22, True, 3.5),
    ("window reset -- the session resumes itself", 3, 42, 34, 289, False, 3.5),
]

for f in FRAMES:
    frame(*f)
