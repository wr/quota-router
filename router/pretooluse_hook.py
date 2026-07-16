#!/usr/bin/env python3
"""PreToolUse hook for Agent/Task delegation.

Injects the live quota snapshot + a routing nudge as additionalContext so the
quota gate is UNCONDITIONALLY present at the moment of delegation, rather than
relying on the model remembering to invoke the subagent-router skill. Advisory:
it surfaces data and points at the skill; it does not block the tool.

Always emits valid JSON and exits 0 — a hook must never wedge a delegation.
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROUTER = os.path.join(HOME, ".claude", "quota-router")
CACHE = os.path.join(HOME, ".claude", "quota-cache.json")
CONFIG = os.path.join(ROUTER, "config.json")
PROBE = os.path.join(ROUTER, "codex_quota_probe.py")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _codex():
    try:
        out = subprocess.run([sys.executable, PROBE], capture_output=True,
                             text=True, timeout=4)
        return json.loads(out.stdout) if out.stdout.strip() else {"available": False}
    except Exception:
        return {"available": False}


def _claude_window(cache, key, now, ttl):
    w = cache.get(key)
    if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
        return None
    age = now - w.get("observed_at", 0)
    fresh = w.get("present_in_latest_payload", False) or age <= ttl
    mtr = None
    if isinstance(w.get("resets_at"), (int, float)):
        mtr = max(0, int((w["resets_at"] - now) / 60))
    return {"used": round(w["used_percentage"], 1), "minutes_to_reset": mtr, "fresh": fresh}


def build_context():
    now = time.time()
    cfg = _load(CONFIG, {})
    ttl = cfg.get("claude_cache_ttl_seconds", 90)
    weekly_thr = cfg.get("weekly_protect_pct", 75)
    five_thr = cfg.get("fivehour_soft_pct", 85)
    cache = _load(CACHE, {})

    five = _claude_window(cache, "five_hour", now, ttl)
    seven = _claude_window(cache, "seven_day", now, ttl)
    codex = _codex()

    lines = ["[subagent-router] Live quota at delegation time:"]

    def fmt(w):
        if not w:
            return "unknown"
        r = f"{w['used']}% used"
        if w["minutes_to_reset"] is not None:
            r += f", resets in {w['minutes_to_reset']}m"
        if not w["fresh"]:
            r += " (stale)"
        return r

    lines.append(f"  Claude 5h: {fmt(five)}")
    lines.append(f"  Claude 7d: {fmt(seven)}")
    if codex.get("available"):
        cw = codex.get("windows", {})
        for role, label in (("short", "5h"), ("long", "weekly")):
            w = cw.get(role, {})
            if w.get("present"):
                if w.get("routing_available", True):
                    lines.append(f"  Codex {label}: {round(w['used_percent'],1)}% used, "
                                 f"resets in {w.get('minutes_to_reset')}m")
                else:
                    lines.append(f"  Codex {label}: unknown (stale/rolled-over)")
    else:
        lines.append(f"  Codex: unknown ({codex.get('reason','unavailable')})")

    # binding signals (advisory)
    signals = []
    if seven and seven["fresh"] and seven["used"] > weekly_thr:
        signals.append(f"WEEKLY BINDING (>{weekly_thr}%): protect hard — offload heavy work to Codex, "
                       f"drop Fable, prefer cheaper Claude tiers, Claude fan-out=1.")
    if five and five["fresh"] and five["used"] > five_thr:
        soon = five["minutes_to_reset"] is not None and five["minutes_to_reset"] <= 20
        if soon:
            signals.append(f"5h SOFT (>{five_thr}%) but resets soon: prefer waiting or a lower Claude tier over offloading.")
        else:
            signals.append(f"5h CONSTRAINED (>{five_thr}%): drop a Claude tier first, else offload to Codex.")
    ov = cfg.get("test_override")
    if isinstance(ov, dict) and ov.get("expires_epoch", 0) > now:
        signals.append("NOTE: an active test_override is in effect — this is a TEST routing decision.")

    if signals:
        lines.append("  Signals: " + " ".join(signals))
    lines.append("  → Before choosing provider/model/effort/fan-out, apply the subagent-router "
                 "skill's gates. Codex must launch via `codex-companion.mjs task --background "
                 "--model <m> --effort <e>` (companion broker path), read-only unless writes are "
                 "intended, with a stated evidence/time budget.")
    return "\n".join(lines)


def main():
    # consume stdin (hook payload) but we don't need its contents
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        ctx = build_context()
    except Exception as exc:
        ctx = f"[subagent-router] quota snapshot unavailable ({type(exc).__name__}); route conservatively."
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": ctx,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
