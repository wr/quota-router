#!/usr/bin/env python3
"""Stop/Notification hook: detect a usage-limit stop and arm hibernation.

The exact event Claude Code emits when a subscription window caps out is
undocumented and shifts across releases, so this hook does two things:

  1. Logs a compact line for every event it sees to events.log (ring-capped) —
     evidence to tune the detection regex against real cap events.
  2. If the event looks like a usage-limit stop AND the quota cache agrees a
     window is actually near its cap, writes hibernate.json and spawns the
     watchdog, which waits out the reset and resumes the session.

The cache cross-check matters: a session that merely *talks about* usage
limits must not arm hibernation. Matching is also restricted to the event's
own message plus the last two transcript lines for the same reason.

Never blocks anything, never prints hook output, always exits 0.
"""
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROUTER = os.path.join(HOME, ".claude", "quota-router")
CACHE = os.path.join(HOME, ".claude", "quota-cache.json")
CONFIG = os.path.join(ROUTER, "config.json")
EVENTS = os.path.join(ROUTER, "events.log")
MARKER = os.path.join(ROUTER, "hibernate.json")
WATCHDOG = os.path.join(ROUTER, "hibernate_watchdog.py")

EVENTS_MAX_LINES = 400
LIMIT_RE = re.compile(
    r"(hit your (session|usage|5.?hour|weekly) limit"
    r"|(usage|session) limit (reached|hit)"
    r"|limit will reset|limit resets? at"
    r"|out of (usage|quota))", re.I)


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _log_event(evt):
    try:
        line = json.dumps({"t": int(time.time()), **{
            k: evt.get(k) for k in
            ("hook_event_name", "message", "reason", "session_id")}})
        lines = []
        if os.path.exists(EVENTS):
            with open(EVENTS) as f:
                lines = f.read().splitlines()[-(EVENTS_MAX_LINES - 1):]
        lines.append(line)
        tmp = EVENTS + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, EVENTS)
    except Exception:
        pass


def _limit_text(evt):
    """Text worth scanning: the event's own message/reason fields, plus (for
    Stop events only) the last two transcript lines."""
    parts = [str(evt.get("message") or ""), str(evt.get("reason") or "")]
    tp = evt.get("transcript_path")
    if evt.get("hook_event_name") == "Stop" and tp and os.path.exists(tp):
        try:
            with open(tp, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 16384))
                tail = f.read().decode("utf-8", "replace").splitlines()
            parts.extend(tail[-2:])
        except Exception:
            pass
    return "\n".join(parts)


def _arm_window(cfg, now):
    """Return (resets_at, window_name) only if a window is actually near its
    cap and resets within the max wait. Otherwise the limit phrase was noise."""
    cache = _load(CACHE, {})
    max_wait = cfg.get("hibernate_max_wait_hours", 12) * 3600
    gates = (("five_hour", cfg.get("fivehour_soft_pct", 85) - 5),
             ("seven_day", cfg.get("weekly_protect_pct", 75)))
    for key, thr in gates:
        w = cache.get(key)
        if not isinstance(w, dict):
            continue
        used, ra = w.get("used_percentage"), w.get("resets_at")
        if isinstance(used, (int, float)) and used >= thr \
                and isinstance(ra, (int, float)) and 0 < ra - now <= max_wait:
            return ra, key
    return None, None


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        pass
    try:
        evt = json.loads(raw) if raw.strip() else {}
    except Exception:
        evt = {}
    _log_event(evt)

    cfg = _load(CONFIG, {})
    if not cfg.get("hibernate_enabled"):
        return 0
    if os.path.exists(MARKER):
        # stale marker (watchdog died / reboot) may be replaced; live one wins
        old = _load(MARKER, {})
        age = time.time() - old.get("armed_at", 0)
        if age <= cfg.get("hibernate_max_wait_hours", 12) * 3600:
            return 0
    if not LIMIT_RE.search(_limit_text(evt)):
        return 0
    now = time.time()
    resets_at, window = _arm_window(cfg, now)
    if resets_at is None:
        return 0

    marker = {
        "armed_at": now,
        "window": window,
        "resets_at": resets_at,
        "session_id": evt.get("session_id"),
        "cwd": evt.get("cwd"),
        "transcript_path": evt.get("transcript_path"),
        "tmux_pane": os.environ.get("TMUX_PANE"),
        "trigger_event": evt.get("hook_event_name"),
    }
    tmp = MARKER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(marker, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, MARKER)

    if not os.environ.get("QUOTA_HIBERNATE_NO_SPAWN"):
        subprocess.Popen([sys.executable, WATCHDOG, "--once"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test():
    global CACHE, CONFIG, EVENTS, MARKER
    import tempfile
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        print(("PASS " if cond else "FAIL ") + name)
        if cond:
            passed += 1
        else:
            failed += 1

    def run_event(evt):
        """Simulate main() on a crafted event without touching stdin."""
        _log_event(evt)
        cfg = _load(CONFIG, {})
        if not cfg.get("hibernate_enabled"):
            return False
        if os.path.exists(MARKER):
            return False
        if not LIMIT_RE.search(_limit_text(evt)):
            return False
        ra, window = _arm_window(cfg, time.time())
        if ra is None:
            return False
        with open(MARKER, "w") as f:
            json.dump({"armed_at": time.time(), "window": window,
                       "resets_at": ra}, f)
        return True

    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        CACHE = os.path.join(d, "cache.json")
        CONFIG = os.path.join(d, "config.json")
        EVENTS = os.path.join(d, "events.log")
        MARKER = os.path.join(d, "hibernate.json")

        def write(path, obj):
            with open(path, "w") as f:
                json.dump(obj, f)

        write(CONFIG, {"hibernate_enabled": True, "fivehour_soft_pct": 85,
                       "weekly_protect_pct": 75, "hibernate_max_wait_hours": 12})

        # 1. limit message + 5h actually near cap -> armed on five_hour
        write(CACHE, {"five_hour": {"used_percentage": 92, "resets_at": now + 1800},
                      "seven_day": {"used_percentage": 40, "resets_at": now + 400000}})
        armed = run_event({"hook_event_name": "Notification",
                           "message": "You've hit your usage limit. It resets at 3pm."})
        m = _load(MARKER, {})
        check("armed_on_five_hour", armed and m.get("window") == "five_hour")
        os.unlink(MARKER)

        # 2. same message but no window near cap -> NOT armed (discussion guard)
        write(CACHE, {"five_hour": {"used_percentage": 30, "resets_at": now + 1800},
                      "seven_day": {"used_percentage": 10, "resets_at": now + 400000}})
        check("not_armed_when_quota_low", not run_event(
            {"hook_event_name": "Notification",
             "message": "You've hit your usage limit."}))

        # 3. unrelated notification -> not armed
        write(CACHE, {"five_hour": {"used_percentage": 92, "resets_at": now + 1800}})
        check("not_armed_unrelated", not run_event(
            {"hook_event_name": "Notification", "message": "Claude needs permission"}))

        # 4. Stop event: limit phrase only in OLD transcript lines -> not armed
        tp = os.path.join(d, "transcript.jsonl")
        with open(tp, "w") as f:
            f.write('{"text":"we talked about the usage limit reached earlier"}\n')
            f.write('{"text":"normal turn"}\n{"text":"another normal turn"}\n')
            f.write('{"text":"final normal line"}\n')
        check("not_armed_old_transcript_mention", not run_event(
            {"hook_event_name": "Stop", "transcript_path": tp}))

        # 5. Stop event: limit phrase in the LAST transcript line -> armed
        with open(tp, "a") as f:
            f.write('{"error":"You have hit your session limit, resets at 6pm"}\n')
        check("armed_from_transcript_tail", run_event(
            {"hook_event_name": "Stop", "transcript_path": tp}))
        os.unlink(MARKER)

        # 6. hibernate disabled -> never armed
        write(CONFIG, {"hibernate_enabled": False})
        check("not_armed_when_disabled", not run_event(
            {"hook_event_name": "Notification",
             "message": "You've hit your usage limit."}))

        # 7. events.log is ring-capped
        for i in range(EVENTS_MAX_LINES + 60):
            _log_event({"hook_event_name": "Notification", "message": f"e{i}"})
        with open(EVENTS) as f:
            n = len(f.read().splitlines())
        check("events_ring_capped", n <= EVENTS_MAX_LINES)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    sys.exit(main())
