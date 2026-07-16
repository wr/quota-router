#!/usr/bin/env python3
"""Plays the README demo storyline in the terminal. Recorded with vhs:

    cd docs/demo && vhs demo.tape

Uses the real statusline renderer, so the GIF shows exactly what ships.
The git segment and accents are pinned per frame so the story is stable
regardless of the state of any real repo.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "router"))
import statusline as sl  # noqa: E402

CFG = {"statusline_style": "minimal", "statusline_color": True,
       "statusline_show_pct": 75, "weekly_protect_pct": 75,
       "fivehour_soft_pct": 85, "claude_cache_ttl_seconds": 90}
DIM, RESET = "\033[2m", "\033[0m"
sl._effort = lambda: "high"

MOJITO_GREEN, SHOP_INDIGO = "#4cb782", "#5e6ad2"


def frame(caption, accent, place, five, codex, mins, codex_mins, hibernating, hold, agents=""):
    now = time.time()
    cache = {"five_hour": {"used_percentage": five, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + mins * 60},
             "seven_day": {"used_percentage": 41, "observed_at": now,
                           "present_in_latest_payload": True,
                           "resets_at": now + 4000 * 60}}
    payload = {"model": {"display_name": "Fable 5"},
               "workspace": {"current_dir": "/x"}}
    sl._accent_hex = lambda cfg, cwd, _a=accent: _a
    sl._git_segment = lambda cwd, cfg, _p=place: _p
    sl._agents_segment = lambda cache, now, eff, _a=agents: _a
    sl._load_json_from_probe = lambda: {
        "available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": float(codex), "minutes_to_reset": codex_mins}}}
    # /dev/null exists, so the renderer sees a hibernation marker
    sl.HIBERNATE_MARKER = "/dev/null" if hibernating else os.path.join(HERE, "none")
    line = sl._render(cache, CFG, now, payload)
    sys.stdout.write("\033[2J\033[H\n")
    sys.stdout.write(f"  {DIM}{caption}{RESET}\n\n")
    sys.stdout.write(f"  {line}\n")
    sys.stdout.flush()
    time.sleep(hold)


FRAMES = [
    ("quiet: model + repo state, in the repo's theme color",
     MOJITO_GREEN, "􀐞 mojito 􀜞 main", 42, 15, 200, 6000, False, 3.2),
    ("another repo, its own accent -- on a branch, uncommitted changes",
     SHOP_INDIGO, "􀐞 shop 􀫲 pay-links", 58, 16, 170, 6000, False, 3.2),
    ("5-hour window crosses 75%: both pools appear",
     SHOP_INDIGO, "􀐞 shop 􀫲 pay-links", 78, 21, 130, 6000, False, 3.2),
    ("past the gate -- new work routes to codex; PR in review",
     SHOP_INDIGO, "􀐞 shop 􀩄 pay-links #4", 91, 27, 84, 6000, False, 3.4,
     "􀃋 sonnet high  􀃍 sol xhigh"),
    ("capped mid-task -> hibernating until reset",
     SHOP_INDIGO, "􀐞 shop 􀩄 pay-links #4", 99, 34, 22, 162, True, 3.2),
    ("window reset: session resumed -- PR checks green",
     SHOP_INDIGO, "􀐞 shop 􀁣 pay-links #4", 4, 34, 289, 6000, False, 3.4),
]

for f in FRAMES:
    frame(*f)
