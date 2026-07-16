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

CFG = {"statusline_style": "minimal", "statusline_color": True,
       "statusline_show_pct": 75, "statusline_accent": "#D97757",
       "weekly_protect_pct": 75, "fivehour_soft_pct": 85,
       "claude_cache_ttl_seconds": 90}
PAYLOAD = {"model": {"display_name": "Opus 4.8"},
           "workspace": {"current_dir": os.path.expanduser("~") + "/projects"}}
DIM, RESET = "\033[2m", "\033[0m"
sl._effort = lambda: "high"


def frame(caption, five, codex, mins, codex_mins, hibernating, hold):
    now = time.time()
    cache = {"five_hour": {"used_percentage": five, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + mins * 60},
             "seven_day": {"used_percentage": 41, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + 4000 * 60}}
    sl._load_json_from_probe = lambda: {
        "available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": float(codex), "minutes_to_reset": codex_mins}}}
    # /dev/null exists, so the renderer sees a hibernation marker
    sl.HIBERNATE_MARKER = "/dev/null" if hibernating else os.path.join(HERE, "none")
    line = sl._render(cache, CFG, now, PAYLOAD)
    sys.stdout.write("\033[2J\033[H\n")
    sys.stdout.write(f"  {DIM}{caption}{RESET}\n\n")
    sys.stdout.write(f"  {line}\n")
    sys.stdout.flush()
    time.sleep(hold)


FRAMES = [
    ("under control: the statusline stays out of the way",
     42, 15, 200, 6000, False, 3.5),
    ("5-hour window crosses 75%: both pools appear",
     78, 21, 130, 6000, False, 3.5),
    ("past the gate: new work routes to codex",
     91, 27, 84, 6000, False, 3.5),
    ("capped mid-task -> hibernating until reset",
     99, 34, 22, 162, True, 3.5),
    ("window reset: clean line, session resumed",
     4, 34, 289, 6000, False, 3.5),
]

for f in FRAMES:
    frame(*f)
