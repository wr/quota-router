#!/usr/bin/env python3
"""Renders every statusline state as a stacked list for the README image:

    cd docs/demo && vhs demo.tape   # writes ../demo.png

Uses the real statusline renderer, so the image shows exactly what ships.
Git segment, agents, and accents are pinned per state so the list is stable
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


def render(accent, place, five, codex, mins, codex_mins, hibernating, agents=()):
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
    sl._agents_entries = lambda *a, _a=agents, **k: list(_a)
    sl._load_json_from_probe = lambda: {
        "available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": float(codex), "minutes_to_reset": codex_mins}}}
    # /dev/null exists, so the renderer sees a hibernation marker
    sl.HIBERNATE_MARKER = "/dev/null" if hibernating else os.path.join(HERE, "none")
    return sl._render(cache, CFG, now, payload)


STATES = [
    ("quiet -- under 75%, quota stays out of the way",
     dict(accent=MOJITO_GREEN, place="􀐞 mojito 􀜞 main",
          five=42, codex=15, mins=200, codex_mins=6000, hibernating=False)),
    ("another repo, its own theme accent -- branch, uncommitted changes",
     dict(accent=SHOP_INDIGO, place="􀐞 shop 􀫲 pay-links",
          five=58, codex=16, mins=170, codex_mins=6000, hibernating=False)),
    ("5-hour window crosses 75%: both pools appear",
     dict(accent=SHOP_INDIGO, place="􀐞 shop 􀫲 pay-links",
          five=78, codex=21, mins=130, codex_mins=6000, hibernating=False)),
    ("past the gate: subagents fan out to codex; PR in review",
     dict(accent=SHOP_INDIGO, place="􀐞 shop 􀩄 pay-links #4",
          five=91, codex=27, mins=84, codex_mins=6000, hibernating=False,
          agents=(("fable-5", "max"), ("sol", "xhigh")))),
    ("capped mid-task -> hibernating until reset",
     dict(accent=SHOP_INDIGO, place="􀐞 shop 􀩄 pay-links #4",
          five=99, codex=34, mins=22, codex_mins=162, hibernating=True)),
    ("window reset: session resumed -- PR checks green",
     dict(accent=SHOP_INDIGO, place="􀐞 shop 􀁣 pay-links #4",
          five=4, codex=34, mins=289, codex_mins=6000, hibernating=False)),
]

sys.stdout.write("\033[2J\033[H")
for caption, kw in STATES:
    sys.stdout.write(f"\n  {DIM}{caption}{RESET}\n")
    sys.stdout.write(f"  {render(**kw)}\n")
sys.stdout.flush()
