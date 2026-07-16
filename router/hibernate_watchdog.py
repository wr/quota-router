#!/usr/bin/env python3
"""Waits out a quota-window reset, then resumes the hibernated session.

Spawned detached by hibernate_hook.py when a usage-limit stop is detected.
Runs a wall-clock loop (60s ticks), so a laptop sleeping through the reset
just delays the check instead of breaking it. Resume prefers typing into the
original tmux pane — the visible session simply continues; the fallback is a
headless `claude --resume`. The marker is single-shot: whatever the outcome,
it is archived to last-hibernate.json so the statusline ⏾ clears and nothing
fires twice.

Manual controls: --status prints the armed marker, --disarm cancels it,
--dry-run shows what a firing would do.
"""
import json
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROUTER = os.path.join(HOME, ".claude", "quota-router")
CONFIG = os.path.join(ROUTER, "config.json")
MARKER = os.path.join(ROUTER, "hibernate.json")
ARCHIVE = os.path.join(ROUTER, "last-hibernate.json")
LOG = os.path.join(ROUTER, "hibernate.log")

RESUME_PROMPT = ("The usage-limit window has reset. Continue the task you were "
                 "working on when the limit interrupted you.")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _archive(marker, outcome):
    done = dict(marker)
    done["outcome"] = outcome
    done["finished_at"] = time.time()
    try:
        with open(ARCHIVE, "w") as f:
            json.dump(done, f, indent=2)
        os.chmod(ARCHIVE, 0o600)
    except Exception:
        pass
    try:
        os.unlink(MARKER)
    except OSError:
        pass


def _pane_alive(pane):
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5)
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        return False


def _resume(marker, dry):
    pane = marker.get("tmux_pane")
    if pane and shutil.which("tmux") and _pane_alive(pane):
        if dry:
            print(f"DRY: tmux send-keys -t {pane} <resume prompt> + Enter")
            return "dry_tmux"
        subprocess.run(["tmux", "send-keys", "-t", pane, RESUME_PROMPT], timeout=5)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], timeout=5)
        return "tmux"
    sid = marker.get("session_id")
    cwd = marker.get("cwd")
    if sid and shutil.which("claude"):
        if dry:
            print(f"DRY: claude --resume {sid} -p <resume prompt> (cwd={cwd})")
            return "dry_headless"
        with open(LOG, "a") as logf:
            subprocess.Popen(["claude", "--resume", sid, "-p", RESUME_PROMPT],
                             cwd=cwd if cwd and os.path.isdir(cwd) else None,
                             stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        return "headless"
    return "no_resume_path"


def run_once(dry=False):
    cfg = _load(CONFIG, {})
    settle = cfg.get("hibernate_settle_seconds", 90)
    max_wait = cfg.get("hibernate_max_wait_hours", 12) * 3600
    while True:
        marker = _load(MARKER, None)
        if not isinstance(marker, dict):
            _log("marker gone; exiting")
            return "disarmed"
        now = time.time()
        if now - marker.get("armed_at", now) > max_wait:
            _log("marker exceeded max wait; abandoning")
            _archive(marker, "abandoned_stale")
            return "abandoned_stale"
        target = marker.get("resets_at", 0) + settle
        if now >= target:
            outcome = _resume(marker, dry)
            _log(f"resume attempted: {outcome} "
                 f"(window={marker.get('window')}, session={marker.get('session_id')})")
            if not dry:
                _archive(marker, outcome)
            return outcome
        if dry:
            print(f"DRY: armed for {marker.get('window')}, "
                  f"would wait {int(target - now)}s more")
            return "dry_waiting"
        time.sleep(max(1, min(60, target - now)))


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    if "--status" in args:
        m = _load(MARKER, None)
        print(json.dumps(m, indent=2) if m else "not hibernating")
        return 0
    if "--disarm" in args:
        m = _load(MARKER, None)
        if m:
            _archive(m, "disarmed_manually")
            print("disarmed")
        else:
            print("nothing armed")
        return 0
    run_once(dry="--dry-run" in args)
    return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test():
    global CONFIG, MARKER, ARCHIVE, LOG
    import tempfile
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        print(("PASS " if cond else "FAIL ") + name)
        if cond:
            passed += 1
        else:
            failed += 1

    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        CONFIG = os.path.join(d, "config.json")
        MARKER = os.path.join(d, "hibernate.json")
        ARCHIVE = os.path.join(d, "last-hibernate.json")
        LOG = os.path.join(d, "hibernate.log")

        def write_marker(m):
            with open(MARKER, "w") as f:
                json.dump(m, f)

        with open(CONFIG, "w") as f:
            json.dump({"hibernate_settle_seconds": 5,
                       "hibernate_max_wait_hours": 12}, f)

        # 1. no marker -> disarmed
        check("no_marker_disarmed", run_once(dry=True) == "disarmed")

        # 2. future reset -> waiting (dry mode returns instead of sleeping)
        write_marker({"armed_at": now, "resets_at": now + 3600, "window": "five_hour"})
        check("future_reset_waits", run_once(dry=True) == "dry_waiting")

        # 3. past reset, no tmux pane / no claude session -> no_resume_path
        write_marker({"armed_at": now, "resets_at": now - 600, "window": "five_hour"})
        saved_which = shutil.which
        shutil.which = lambda *_: None
        try:
            check("past_reset_no_path", run_once(dry=True) == "no_resume_path")
        finally:
            shutil.which = saved_which

        # 4. past reset with session id and a fake `claude` -> dry_headless
        write_marker({"armed_at": now, "resets_at": now - 600,
                      "window": "five_hour", "session_id": "abc123", "cwd": d})
        shutil.which = lambda name: "/usr/bin/true" if name == "claude" else None
        try:
            check("past_reset_headless_dry", run_once(dry=True) == "dry_headless")
        finally:
            shutil.which = saved_which

        # 5. stale marker -> abandoned + archived + marker removed
        write_marker({"armed_at": now - 24 * 3600, "resets_at": now - 20 * 3600,
                      "window": "five_hour"})
        r = run_once(dry=True)
        check("stale_abandoned", r == "abandoned_stale"
              and not os.path.exists(MARKER) and os.path.exists(ARCHIVE))

        # 6. disarm flow
        write_marker({"armed_at": now, "resets_at": now + 3600})
        m = _load(MARKER, None)
        _archive(m, "disarmed_manually")
        check("disarm_removes_marker", not os.path.exists(MARKER)
              and _load(ARCHIVE, {}).get("outcome") == "disarmed_manually")

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
