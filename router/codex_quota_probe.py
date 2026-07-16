#!/usr/bin/env python3
"""Read-only Codex quota probe.

Reads the newest valid `token_count` rate-limit snapshot from Codex rollout
JSONL files (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) and emits a
normalized JSON snapshot. Never invokes Codex, never spends quota, never
writes anything.

Windows are classified by window_minutes ("short" <=360min, "long" >360min)
rather than by primary/secondary, because plans differ (on some plans the
weekly window is `primary`).

A reset that has already passed is reported as routing-unavailable (unknown),
NOT as 0% used — a stale snapshot says nothing about usage accumulated after
the reset, possibly in another session.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

SHORT_MAX_MINUTES = 360          # <=6h counts as a "short" (5h-style) window
MAX_TOTAL_SCAN_BYTES = 4 * 1024 * 1024
MAX_FILES_SCANNED = 8            # bounded set, newest-mtime first
TAIL_CHUNK = 256 * 1024


def _parse_iso_epoch(ts):
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() \
            if datetime.fromisoformat(s).tzinfo is None \
            else datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _iter_tail_lines(path, budget):
    """Yield complete lines from the end of `path`, newest-first, up to `budget`
    bytes. Skips a partial trailing line. Returns bytes actually read."""
    read = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    with open(path, "rb") as f:
        pos = size
        carry = b""
        first = True
        while pos > 0 and read < budget:
            step = min(TAIL_CHUNK, pos, budget - read)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            read += step
            data = chunk + carry
            parts = data.split(b"\n")
            carry = parts[0]  # may be incomplete until we read further back
            tail = parts[1:]
            if first:
                # drop a partial final line (no trailing newline written yet)
                if tail and not data.endswith(b"\n"):
                    tail = tail[:-1] if len(tail) >= 1 else tail
                first = False
            for ln in reversed(tail):
                if ln.strip():
                    yield ln
        if pos == 0 and carry.strip():
            yield carry
    return read


def _extract_event(line):
    try:
        e = json.loads(line)
    except Exception:
        return None
    if e.get("type") != "event_msg":
        return None
    payload = e.get("payload") or {}
    if payload.get("type") != "token_count":
        return None
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    return e, rl


def _norm_window(win, snap_epoch, now):
    """Normalize one window dict -> normalized dict or None if unusable."""
    if not isinstance(win, dict):
        return None
    used = win.get("used_percent")
    wmin = win.get("window_minutes")
    if not isinstance(used, (int, float)) or not isinstance(wmin, (int, float)):
        return None
    reset_epoch = win.get("resets_at")
    if not isinstance(reset_epoch, (int, float)):
        secs = win.get("resets_in_seconds")
        reset_epoch = snap_epoch + secs if isinstance(secs, (int, float)) and snap_epoch else None
    role = "short" if wmin <= SHORT_MAX_MINUTES else "long"
    out = {
        "present": True,
        "role": role,
        "used_percent": float(used),
        "window_minutes": float(wmin),
        "routing_available": True,
        "stale": False,
        "minutes_to_reset": None,
    }
    if reset_epoch is None:
        # No reset info: usable for level but not for wait/relief decisions.
        out["minutes_to_reset"] = None
        return out
    # implausible reset far in the future (> 2 window lengths) => invalid
    if reset_epoch - now > wmin * 60 * 2 + 3600:
        out["routing_available"] = False
        out["stale"] = True
        out["reason"] = "implausible_reset"
        out["minutes_to_reset"] = None
        return out
    delta = reset_epoch - now
    if delta <= 0:
        # rolled over since snapshot: unknown for routing, NOT zero headroom
        out["routing_available"] = False
        out["stale"] = True
        out["minutes_to_reset"] = 0
        out["reason"] = "rolled_over"
        return out
    import math
    out["minutes_to_reset"] = int(math.ceil(delta / 60.0))
    return out


def probe(sessions_root, now):
    root = os.path.expanduser(sessions_root)
    if not os.path.isdir(root):
        return {"provider": "codex", "available": False, "reason": "no_session_dir"}
    files = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    if not files:
        return {"provider": "codex", "available": False, "reason": "no_rollout"}
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    scanned = 0
    for path in files[:MAX_FILES_SCANNED]:
        if scanned >= MAX_TOTAL_SCAN_BYTES:
            break
        budget = min(TAIL_CHUNK * 8, MAX_TOTAL_SCAN_BYTES - scanned)
        found = None
        for line in _iter_tail_lines(path, budget):
            scanned += len(line)
            ev = _extract_event(line)
            if ev:
                found = (path, ev[0], ev[1])
                break
        if found:
            path, event, rl = found
            snap_epoch = _parse_iso_epoch(event.get("timestamp"))
            used_mtime = False
            if snap_epoch is None:
                snap_epoch = os.path.getmtime(path)
                used_mtime = True
            windows = {"short": {"present": False}, "long": {"present": False}}
            for key in ("primary", "secondary"):
                nw = _norm_window(rl.get(key), snap_epoch, now)
                if nw:
                    windows[nw["role"]] = nw
            return {
                "provider": "codex",
                "available": True,
                "reason": "ok",
                "source_file": path,
                "snapshot_epoch": snap_epoch,
                "snapshot_age_seconds": int(max(0, now - snap_epoch)),
                "used_mtime_fallback": used_mtime,
                "plan_type": rl.get("plan_type"),
                "windows": windows,
            }
    return {"provider": "codex", "available": False, "reason": "no_rate_limit_event"}


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _run_self_test():
    import tempfile
    import math
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"PASS {name}")
        else:
            failed += 1
            print(f"FAIL {name}")

    now = 1_800_000_000
    with tempfile.TemporaryDirectory() as d:
        day = os.path.join(d, "2026", "07", "15")
        os.makedirs(day)

        def write_rollout(name, events, mtime=None):
            p = os.path.join(day, name)
            with open(p, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
            if mtime:
                os.utime(p, (mtime, mtime))
            return p

        def evt(ts_epoch, primary=None, secondary=None):
            return {
                "timestamp": datetime.fromtimestamp(ts_epoch, timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": {
                    "plan_type": "test", "primary": primary, "secondary": secondary}},
            }

        # 1. primary via resets_at (long/weekly)
        write_rollout("rollout-a.jsonl", [evt(now - 60, primary={
            "used_percent": 19.0, "window_minutes": 10080, "resets_at": now + 3600})],
            mtime=now - 60)
        r = probe(d, now)
        check("primary_resets_at_long", r["available"] and r["windows"]["long"]["present"]
              and r["windows"]["long"]["minutes_to_reset"] == 60
              and r["windows"]["short"]["present"] is False)

        # 2. primary via resets_in_seconds (short window)
        write_rollout("rollout-b.jsonl", [evt(now - 60, primary={
            "used_percent": 50.0, "window_minutes": 300, "resets_in_seconds": 1200})],
            mtime=now - 30)
        r = probe(d, now)
        check("primary_resets_in_seconds_short", r["windows"]["short"]["present"]
              and r["windows"]["short"]["minutes_to_reset"] == math.ceil((now - 60 + 1200 - now) / 60))

        # 3. valid secondary
        write_rollout("rollout-c.jsonl", [evt(now - 10,
            primary={"used_percent": 10.0, "window_minutes": 10080, "resets_at": now + 7200},
            secondary={"used_percent": 40.0, "window_minutes": 300, "resets_at": now + 600})],
            mtime=now - 10)
        r = probe(d, now)
        check("valid_secondary_short", r["windows"]["short"]["present"]
              and r["windows"]["long"]["present"])

        # 4. secondary null
        write_rollout("rollout-d.jsonl", [evt(now - 5,
            primary={"used_percent": 12.0, "window_minutes": 10080, "resets_at": now + 100},
            secondary=None)], mtime=now - 5)
        r = probe(d, now)
        check("secondary_null", r["windows"]["long"]["present"]
              and r["windows"]["short"]["present"] is False)

        # 5. rolled-over reset -> unknown, NOT zero headroom
        write_rollout("rollout-e.jsonl", [evt(now - 5,
            primary={"used_percent": 88.0, "window_minutes": 300, "resets_at": now - 10})],
            mtime=now - 3)
        r = probe(d, now)
        w = r["windows"]["short"]
        check("rolled_over_unknown", w["present"] and w["routing_available"] is False
              and w["stale"] is True and w["used_percent"] == 88.0)

        # 6. missing timestamp -> mtime fallback
        e = evt(now - 5, primary={"used_percent": 5.0, "window_minutes": 10080, "resets_at": now + 100})
        del e["timestamp"]
        write_rollout("rollout-f.jsonl", [e], mtime=now - 2)
        r = probe(d, now)
        check("mtime_fallback", r["available"] and r["used_mtime_fallback"] is True)

        # 7. malformed partial final line is skipped
        p = os.path.join(day, "rollout-g.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps(evt(now - 5, primary={
                "used_percent": 7.0, "window_minutes": 10080, "resets_at": now + 100})) + "\n")
            f.write('{"type":"event_msg","payload":{"type":"token_count"')  # truncated, no newline
        os.utime(p, (now - 1, now - 1))
        r = probe(d, now)
        check("malformed_trailing_skipped", r["available"] and r["windows"]["long"]["present"])

        # 8. file with no rate-limit event -> falls back to older valid file
        p = os.path.join(day, "rollout-h.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}) + "\n")
        os.utime(p, (now, now))  # newest, but no rate-limit event
        r = probe(d, now)
        check("skip_file_without_event", r["available"] is True)

    with tempfile.TemporaryDirectory() as empty:
        # 9. no rollout files
        r = probe(empty, now)
        check("no_rollout", r["available"] is False and r["reason"] == "no_rollout")
        r2 = probe(os.path.join(empty, "does-not-exist"), now)
        check("no_session_dir", r2["available"] is False and r2["reason"] == "no_session_dir")

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="Read-only Codex quota probe")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--now", type=float, default=None, help="override clock (epoch secs)")
    ap.add_argument("--sessions-root", default="~/.codex/sessions")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _run_self_test()
    now = args.now if args.now is not None else time.time()
    try:
        result = probe(args.sessions_root, now)
    except Exception as exc:  # never crash a status line
        result = {"provider": "codex", "available": False, "reason": f"probe_error:{type(exc).__name__}"}
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
