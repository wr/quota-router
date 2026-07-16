#!/usr/bin/env python3
"""Claude Code status line: cache the pushed rate_limits, render a quota readout.

Claude Code pipes its status JSON to this script on stdin every tick. We:
  1. Extract only rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}
     and store them (each window independently) in ~/.claude/quota-cache.json.
  2. Render a one-line quota readout (Claude + Codex) to stdout.

Concurrency-safe: unique temp + fsync + os.replace, under an flock, and a new
tick never overwrites a window with an OLDER observation. An absent window is
retained (marked not-present-in-latest), never zeroed. last_tick_at is tracked
separately so a fresh empty tick can't make an old observation look fresh.

If an existing status line was configured at install time, it is recorded in
config.previous_statusline and wrapped (run with the original stdin under a
short timeout), with our readout appended.
"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
ROUTER_DIR = os.path.join(HOME, ".claude", "quota-router")
CACHE = os.path.join(HOME, ".claude", "quota-cache.json")
LOCK = os.path.join(HOME, ".claude", "quota-cache.lock")
CONFIG = os.path.join(ROUTER_DIR, "config.json")
PROBE = os.path.join(ROUTER_DIR, "codex_quota_probe.py")
HIBERNATE_MARKER = os.path.join(ROUTER_DIR, "hibernate.json")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _extract_claude_windows(payload, now):
    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
    out = {}
    if isinstance(rl, dict):
        for key in ("five_hour", "seven_day"):
            w = rl.get(key)
            if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
                out[key] = {
                    "used_percentage": float(w["used_percentage"]),
                    "resets_at": w.get("resets_at"),
                    "observed_at": now,
                    "present_in_latest_payload": True,
                }
    return out


def _update_cache(new_windows, now, cache_dir, meta=None):
    """Read-modify-write the cache under an flock. Carry forward windows absent
    from this tick; never replace a stored window with an older observation."""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cache = _load_json(CACHE, {})
        if not isinstance(cache, dict):
            cache = {}
        merged = {}
        stored_meta = cache.get("meta") if isinstance(cache.get("meta"), dict) else {}
        if isinstance(meta, dict):
            stored_meta = dict(stored_meta, **{k: v for k, v in meta.items() if v})
        if stored_meta:
            merged["meta"] = stored_meta
        for key in ("five_hour", "seven_day"):
            incoming = new_windows.get(key)
            stored = cache.get(key) if isinstance(cache.get(key), dict) else None
            if incoming and stored:
                # reject an older observation than what's already stored
                if incoming["observed_at"] >= stored.get("observed_at", 0):
                    merged[key] = incoming
                else:
                    merged[key] = stored
            elif incoming:
                merged[key] = incoming
            elif stored:
                stored = dict(stored)
                stored["present_in_latest_payload"] = False
                merged[key] = stored
        merged["last_tick_at"] = now
        _atomic_write(CACHE, merged, cache_dir)
        return merged
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _atomic_write(target, obj, cache_dir):
    fd, tmp = tempfile.mkstemp(prefix=".quota-cache-", dir=cache_dir)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fmt_pct(window, now, ttl):
    if not isinstance(window, dict):
        return "--"
    used = window.get("used_percentage")
    if not isinstance(used, (int, float)):
        return "--"
    age = now - window.get("observed_at", 0)
    stale = (not window.get("present_in_latest_payload", False) and age > ttl)
    return f"{int(round(used))}%" + ("~" if stale else "")


def _fmt_reset_suffix(minutes):
    """Countdown like (34m) or (8h20m). Shown only on binding windows — that's
    when the wait-vs-switch call matters."""
    if not isinstance(minutes, (int, float)):
        return ""
    m = int(max(0, minutes))
    return f"({m // 60}h{m % 60:02d}m)" if m >= 60 else f"({m}m)"


# statusline_style config values. "plain" = numbers only.
STYLE_RAMPS = {
    "circles": "○◔◑◕●",
    "braille": "⡀⡄⡆⡇⣇⣧⣷⣿",
}
RED, YELLOW, RESET = "\033[1;31m", "\033[33m", "\033[0m"


def _glyph(style, pct):
    ramp = STYLE_RAMPS.get(style, "")
    if not ramp or not isinstance(pct, (int, float)):
        return ""
    # nearest level, so 15% reads as a quarter circle rather than empty
    idx = int(round(max(0.0, min(100.0, float(pct))) * (len(ramp) - 1) / 100.0))
    return ramp[idx]


def _paint(text, pct, thr, color_on):
    """Yellow approaching a window's own threshold, red+bold past it."""
    if not color_on or not isinstance(pct, (int, float)):
        return text
    if pct > thr:
        return RED + text + RESET
    if pct >= thr - 10:
        return YELLOW + text + RESET
    return text


def _fmt_codex(probe, now, old_secs, weekly_thr, five_thr, style="plain", color_on=False):
    if not probe.get("available"):
        return "Codex --"
    parts = []
    for role, label, thr in (("short", "5h", five_thr), ("long", "wk", weekly_thr)):
        w = probe.get("windows", {}).get(role, {})
        if not w.get("present"):
            continue
        if not w.get("routing_available", True):
            parts.append(f"{label} --")
            continue
        mark = "~" if probe.get("snapshot_age_seconds", 0) > old_secs else ""
        flag = ""
        if w["used_percent"] > thr:
            flag = "!" + _fmt_reset_suffix(w.get("minutes_to_reset"))
        chunk = _glyph(style, w["used_percent"]) \
            + f"{int(round(w['used_percent']))}%{flag}{mark}"
        parts.append(f"{label} " + _paint(chunk, w["used_percent"], thr, color_on))
    return "Codex " + (" ".join(parts) if parts else "--")


def _hex_to_ansi(hexstr):
    try:
        h = str(hexstr).lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[38;2;{r};{g};{b}m"
    except Exception:
        return ""


def _effort():
    s = _load_json(os.path.join(HOME, ".claude", "settings.json"), {})
    e = s.get("effortLevel")
    return e if isinstance(e, str) and e else None


def _accent_hex(config, cwd):
    """Per-repo accent. First hit wins: STATUSLINE_ACCENT env var, then a
    `statusline_accent` key in .claude/settings.local.json or settings.json
    walking up from the session's cwd, then the router config, then coral."""
    env = os.environ.get("STATUSLINE_ACCENT")
    if env:
        return env
    d = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
    while d and d != "/":
        for name in ("settings.local.json", "settings.json"):
            v = _load_json(os.path.join(d, ".claude", name), {}).get("statusline_accent")
            if isinstance(v, str) and v:
                return v
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return config.get("statusline_accent", "#D97757")


def _render_minimal(cache, config, now, payload):
    """`opus-4.8 high · ~/projects` — quota is invisible until a provider
    crosses statusline_show_pct; then both providers' numbers appear, with a
    reset countdown when the reset is near enough to be actionable."""
    color_on = bool(config.get("statusline_color", True))
    show_thr = config.get("statusline_show_pct", 75)
    weekly_thr = config.get("weekly_protect_pct", 75)
    five_thr = config.get("fivehour_soft_pct", 85)
    ttl = config.get("claude_cache_ttl_seconds", 90)
    old_secs = config.get("codex_old_snapshot_seconds", 1800)
    GRAY, WHITE = "\033[90m", "\033[97m"

    def c(code, text):
        return f"{code}{text}{RESET}" if color_on and code else text

    meta = {}
    if isinstance(payload, dict):
        m = payload.get("model") or {}
        ws = payload.get("workspace") or {}
        meta = {"model": m.get("display_name") or m.get("id"),
                "cwd": ws.get("current_dir") or payload.get("cwd")}
    stored = cache.get("meta") if isinstance(cache.get("meta"), dict) else {}
    model = str(meta.get("model") or stored.get("model") or "claude")
    model = model.lower().replace("claude-", "").replace(" ", "-")
    effort = _effort()
    head = model + (f" {effort}" if effort else "")
    cwd_raw = str(meta.get("cwd") or stored.get("cwd") or "")
    accent = _hex_to_ansi(_accent_hex(config, cwd_raw))
    cwd = "~" + cwd_raw[len(HOME):] if cwd_raw.startswith(HOME) else cwd_raw

    def claude_pick():
        best = None
        for key, thr in (("five_hour", five_thr), ("seven_day", weekly_thr)):
            w = cache.get(key)
            if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
                continue
            stale = (not w.get("present_in_latest_payload", False)
                     and now - w.get("observed_at", 0) > ttl)
            mtr = (w["resets_at"] - now) / 60 \
                if isinstance(w.get("resets_at"), (int, float)) else None
            if best is None or w["used_percentage"] > best[0]:
                best = (w["used_percentage"], thr, mtr, stale)
        return best

    probe = _load_json_from_probe()

    def codex_pick():
        if not probe.get("available"):
            return None
        best = None
        for role, thr in (("short", five_thr), ("long", weekly_thr)):
            w = probe.get("windows", {}).get(role, {})
            if not w.get("present") or not w.get("routing_available", True):
                continue
            stale = probe.get("snapshot_age_seconds", 0) > old_secs
            if best is None or w["used_percent"] > best[0]:
                best = (w["used_percent"], thr, w.get("minutes_to_reset"), stale)
        return best

    def pct_str(pick):
        used, thr, mtr, stale = pick
        txt = f"{int(round(used))}%" + ("~" if stale else "")
        if isinstance(mtr, (int, float)) and 0 < mtr <= 480:
            txt += " " + _fmt_reset_suffix(mtr)
        code = RED if used > thr else (YELLOW if used >= show_thr else "")
        return c(code, txt)

    cl, cx = claude_pick(), codex_pick()
    constrained = (cl and cl[0] >= show_thr) or (cx and cx[0] >= show_thr)

    parts = []
    if constrained:
        parts.append(c(accent, head) + " " + pct_str(cl) if cl else c(accent, head))
        parts.append(c(WHITE, "codex") + " " + (pct_str(cx) if cx else "--"))
    else:
        parts.append(c(accent, head))
    if cwd:
        parts.append(c(GRAY, cwd))
    prefix = "⏾ " if os.path.exists(HIBERNATE_MARKER) else ""
    return prefix + c(GRAY, " · ").join(parts) if color_on \
        else prefix + " · ".join(parts)


def _render(cache, config, now, payload=None):
    if config.get("statusline_style") == "minimal":
        return _render_minimal(cache, config, now, payload)
    ttl = config.get("claude_cache_ttl_seconds", 90)
    old_secs = config.get("codex_old_snapshot_seconds", 1800)
    weekly_thr = config.get("weekly_protect_pct", 75)
    five_thr = config.get("fivehour_soft_pct", 85)
    style = config.get("statusline_style", "plain")
    color_on = bool(config.get("statusline_color", False))

    # binding flags (display only)
    def binding(win, thr):
        return isinstance(win, dict) and isinstance(win.get("used_percentage"), (int, float)) \
            and win["used_percentage"] > thr

    def claude_part(key, thr):
        win = cache.get(key)
        text = _fmt_pct(win, now, ttl)
        pct = win.get("used_percentage") if isinstance(win, dict) else None
        if binding(win, thr):
            mtr = (win["resets_at"] - now) / 60 \
                if isinstance(win.get("resets_at"), (int, float)) else None
            text += "!" + _fmt_reset_suffix(mtr)
        return _paint(_glyph(style, pct) + text, pct, thr, color_on)

    five = claude_part("five_hour", five_thr)
    seven = claude_part("seven_day", weekly_thr)
    probe = _load_json_from_probe()
    codex = _fmt_codex(probe, now, old_secs, weekly_thr, five_thr, style, color_on)
    prefix = "⏾ " if os.path.exists(HIBERNATE_MARKER) else ""
    return f"{prefix}Claude 5h {five} / 7d {seven} · {codex}"


def _load_json_from_probe():
    try:
        out = subprocess.run(
            [sys.executable, PROBE], capture_output=True, text=True, timeout=4)
        return json.loads(out.stdout) if out.stdout.strip() else {"available": False}
    except Exception:
        return {"available": False}


def _maybe_wrap_previous(config, raw_stdin):
    prev = config.get("previous_statusline")
    if not isinstance(prev, dict):
        return None
    cmd = prev.get("command")
    if not cmd:
        return None
    # avoid self-recursion by resolved path
    try:
        if os.path.realpath(cmd) == os.path.realpath(__file__):
            return None
    except Exception:
        pass
    try:
        out = subprocess.run(cmd, shell=True, input=raw_stdin, capture_output=True,
                             text=True, timeout=1)
        line = out.stdout.strip()
        return line or None
    except Exception:
        return None


def main():
    now = time.time()
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    payload = _load_json_from_str(raw)
    config = _load_json(CONFIG, {})
    cache_dir = os.path.dirname(CACHE)
    meta = None
    if isinstance(payload, dict):
        m = payload.get("model") or {}
        ws = payload.get("workspace") or {}
        meta = {"model": m.get("display_name") or m.get("id"),
                "cwd": ws.get("current_dir") or payload.get("cwd")}
    try:
        windows = _extract_claude_windows(payload, now)
        cache = _update_cache(windows, now, cache_dir, meta)
    except Exception:
        cache = _load_json(CACHE, {})  # degrade: still render from last cache
    readout = _render(cache, config, now, payload)
    prev_line = _maybe_wrap_previous(config, raw)
    sys.stdout.write((prev_line + "  " if prev_line else "") + readout)
    return 0


def _load_json_from_str(s):
    try:
        return json.loads(s) if s.strip() else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test():
    global CACHE, LOCK
    import io
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        print(("PASS " if cond else "FAIL ") + name)
        if cond:
            passed += 1
        else:
            failed += 1

    with tempfile.TemporaryDirectory() as d:
        CACHE = os.path.join(d, "quota-cache.json")
        LOCK = os.path.join(d, "quota-cache.lock")
        now = 1_800_000_000

        # both windows present
        p = {"rate_limits": {
            "five_hour": {"used_percentage": 64, "resets_at": now + 1000},
            "seven_day": {"used_percentage": 71, "resets_at": now + 500000}}}
        c = _update_cache(_extract_claude_windows(p, now), now, d)
        check("both_present", c["five_hour"]["used_percentage"] == 64
              and c["seven_day"]["used_percentage"] == 71)
        check("perms_0600", (os.stat(CACHE).st_mode & 0o777) == 0o600)

        # next tick omits five_hour -> retained, marked not-present
        p2 = {"rate_limits": {"seven_day": {"used_percentage": 72, "resets_at": now + 500000}}}
        c = _update_cache(_extract_claude_windows(p2, now + 60), now + 60, d)
        check("absent_window_retained", c["five_hour"]["used_percentage"] == 64
              and c["five_hour"]["present_in_latest_payload"] is False)
        check("absent_not_zeroed", c["five_hour"]["used_percentage"] != 0)
        check("last_tick_advanced", c["last_tick_at"] == now + 60)

        # older observation must not overwrite newer stored
        old = _extract_claude_windows(
            {"rate_limits": {"seven_day": {"used_percentage": 5, "resets_at": now}}}, now - 100)
        c = _update_cache(old, now - 100, d)
        check("reject_older_observation", c["seven_day"]["used_percentage"] == 72)

        # invalid input JSON doesn't crash extraction
        check("invalid_json_safe", _extract_claude_windows(_load_json_from_str("{bad"), now) == {})

        # atomic replace leaves no stray temp files
        strays = [f for f in os.listdir(d) if f.startswith(".quota-cache-")]
        check("no_stray_tmp", strays == [])

        # display markers
        cache = {"five_hour": {"used_percentage": 88, "observed_at": now,
                               "present_in_latest_payload": True,
                               "resets_at": now + 34 * 60},
                 "seven_day": {"used_percentage": 40, "observed_at": now - 999,
                               "present_in_latest_payload": False}}
        cfg = {"claude_cache_ttl_seconds": 90, "fivehour_soft_pct": 85, "weekly_protect_pct": 75}
        # bypass the real codex probe in render
        global _load_json_from_probe
        saved = _load_json_from_probe
        _load_json_from_probe = lambda: {"available": False}
        try:
            line = _render(cache, cfg, now)
        finally:
            _load_json_from_probe = saved
        check("stale_marker_and_binding", "88%!(34m)" in line and "40%~" in line)

        # codex binding window gets flag + countdown
        _load_json_from_probe = lambda: {"available": True, "snapshot_age_seconds": 10, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": 91.0, "minutes_to_reset": 500}}}
        try:
            line2 = _render(cache, cfg, now)
        finally:
            _load_json_from_probe = saved
        check("codex_binding_countdown", "wk 91%!(8h20m)" in line2)

        # circles style + color: glyph before the number, red past threshold,
        # no color below thr-10
        cfgc = dict(cfg, statusline_style="circles", statusline_color=True)
        _load_json_from_probe = lambda: {"available": False}
        try:
            line3 = _render(cache, cfgc, now)
        finally:
            _load_json_from_probe = saved
        check("circle_glyph_and_red", "●88%!(34m)" in line3 and RED + "●88%" in line3)
        check("low_window_uncolored", "◑40%~" in line3 and YELLOW + "◑40%" not in line3)
        check("glyph_nearest_level", _glyph("circles", 15) == "◔"
              and _glyph("braille", 88) == "⣷" and _glyph("braille", 5) == "⡀"
              and _glyph("circles", 100) == "●" and _glyph("circles", 0) == "○")

        # hibernate marker prefix
        global HIBERNATE_MARKER
        saved_marker = HIBERNATE_MARKER
        HIBERNATE_MARKER = os.path.join(d, "hibernate.json")
        with open(HIBERNATE_MARKER, "w") as f:
            f.write("{}")
        _load_json_from_probe = lambda: {"available": False}
        try:
            line4 = _render(cache, cfg, now)
        finally:
            _load_json_from_probe = saved
            HIBERNATE_MARKER = saved_marker
        check("hibernate_prefix", line4.startswith("⏾ "))

        # ---- minimal style ----
        global _effort
        saved_eff = _effort
        _effort = lambda: "high"
        cfgm = {"statusline_style": "minimal", "statusline_color": True,
                "weekly_protect_pct": 75, "fivehour_soft_pct": 85,
                "claude_cache_ttl_seconds": 90}
        payload = {"model": {"display_name": "Opus 4.8"},
                   "workspace": {"current_dir": HOME + "/projects"}}
        codex_low = lambda: {"available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": False},
            "long": {"present": True, "routing_available": True,
                     "used_percent": 15.0, "minutes_to_reset": 6000}}}
        codex_hot = lambda: {"available": True, "snapshot_age_seconds": 5, "windows": {
            "short": {"present": True, "routing_available": True,
                      "used_percent": 82.0, "minutes_to_reset": 162},
            "long": {"present": False}}}

        low = {"five_hour": {"used_percentage": 40, "observed_at": now,
                             "present_in_latest_payload": True, "resets_at": now + 3600},
               "seven_day": {"used_percentage": 30, "observed_at": now,
                             "present_in_latest_payload": True, "resets_at": now + 500000}}
        _load_json_from_probe = codex_low
        try:
            lm = _render(low, cfgm, now, payload)
        finally:
            _load_json_from_probe = saved
        check("minimal_quiet", "opus-4.8 high" in lm and "~/projects" in lm
              and "%" not in lm and "codex" not in lm)

        hot = {"five_hour": {"used_percentage": 76, "observed_at": now,
                             "present_in_latest_payload": True, "resets_at": now + 130 * 60},
               "seven_day": {"used_percentage": 30, "observed_at": now,
                             "present_in_latest_payload": True, "resets_at": now + 500000}}
        _load_json_from_probe = codex_hot
        try:
            lh = _render(hot, cfgm, now, payload)
        finally:
            _load_json_from_probe = saved
        check("minimal_constrained", "76% (2h10m)" in lh
              and "codex" in lh and "82% (2h42m)" in lh)
        check("minimal_yellow_not_red", YELLOW + "76%" in lh
              and YELLOW + "82%" in lh and RED not in lh)

        red_cache = dict(hot, five_hour=dict(hot["five_hour"], used_percentage=91))
        _load_json_from_probe = codex_low
        try:
            lr = _render(red_cache, cfgm, now, payload)
        finally:
            _load_json_from_probe = saved
        check("minimal_red_past_threshold", RED + "91%" in lr and "codex" in lr)

        # weekly binding far from reset: pct shown, no countdown on the claude part
        wk = {"seven_day": {"used_percentage": 78, "observed_at": now,
                            "present_in_latest_payload": True, "resets_at": now + 5000 * 60},
              "five_hour": {"used_percentage": 10, "observed_at": now,
                            "present_in_latest_payload": True, "resets_at": now + 3600}}
        _load_json_from_probe = codex_low
        try:
            lw = _render(wk, cfgm, now, payload)
        finally:
            _load_json_from_probe = saved
            _effort = saved_eff
        check("minimal_no_far_countdown", "78%" in lw and "(" not in lw)

        # per-repo accent resolution: project settings beat global config
        proj = os.path.join(d, "proj")
        os.makedirs(os.path.join(proj, ".claude"))
        with open(os.path.join(proj, ".claude", "settings.local.json"), "w") as f:
            json.dump({"statusline_accent": "#6ab586"}, f)
        saved_env = os.environ.pop("STATUSLINE_ACCENT", None)
        try:
            check("accent_from_project",
                  _accent_hex({"statusline_accent": "#D97757"}, proj) == "#6ab586")
            check("accent_walks_up",
                  _accent_hex({}, os.path.join(proj, "src", "deep")) == "#6ab586")
            check("accent_global_fallback",
                  _accent_hex({"statusline_accent": "#123456"}, d) == "#123456")
            os.environ["STATUSLINE_ACCENT"] = "#ffffff"
            check("accent_env_wins", _accent_hex({}, proj) == "#ffffff")
        finally:
            os.environ.pop("STATUSLINE_ACCENT", None)
            if saved_env:
                os.environ["STATUSLINE_ACCENT"] = saved_env

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def _demo():
    """Render sample lines at three pressure levels in every style, with color,
    so a style can be chosen by looking rather than imagining."""
    global _load_json_from_probe
    now = time.time()
    scenarios = [("cruising", 34, 12, 15), ("warming", 78, 68, 35), ("binding", 92, 71, 88)]
    saved = _load_json_from_probe
    for style in ("plain", "circles", "braille"):
        print(f"[{style}]")
        for name, five, seven, codex in scenarios:
            cache = {"five_hour": {"used_percentage": five, "observed_at": now,
                                   "present_in_latest_payload": True, "resets_at": now + 34 * 60},
                     "seven_day": {"used_percentage": seven, "observed_at": now,
                                   "present_in_latest_payload": True, "resets_at": now + 3000 * 60}}
            cfg = {"statusline_style": style, "statusline_color": True}
            _load_json_from_probe = lambda c=codex: {
                "available": True, "snapshot_age_seconds": 5, "windows": {
                    "short": {"present": False},
                    "long": {"present": True, "routing_available": True,
                             "used_percent": float(c), "minutes_to_reset": 4000}}}
            try:
                print(f"  {name:9s} {_render(cache, cfg, now)}")
            finally:
                _load_json_from_probe = saved
        print()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        sys.exit(_demo())
    sys.exit(main())
