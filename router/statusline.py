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
import glob
import json
import os
import re
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


THEMES_DIR = os.path.join(HOME, ".claude", "themes")
PR_CACHE = os.path.join(ROUTER_DIR, "pr-cache.json")
PR_TTL = 180

# SF Symbols (macOS; needs a font stack that includes SF Pro) with a plain
# unicode fallback for everything else. Override via statusline_git_symbols.
GIT_SYMBOLS_SF = {"repo": "􀐞", "main_clean": "􀜞", "main_dirty": "􀧙",
                  "branch_clean": "􀣽", "branch_dirty": "􀫲",
                  "pr_open": "􀩄", "pr_green": "􀁣"}
GIT_SYMBOLS_ASCII = {"repo": "▣", "main_clean": "●", "main_dirty": "◐",
                     "branch_clean": "⎇", "branch_dirty": "±",
                     "pr_open": "◌", "pr_green": "✓"}


def _git(cwd, *args, timeout=2):
    try:
        out = subprocess.run(["git", "-C", cwd] + list(args),
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _pr_state(top, branch):
    """Cached PR state for this branch: 'green', 'open', or None. A stale
    entry triggers a detached background `gh` refresh — ticks never block on
    the network, they render the last known state."""
    key = f"{top}:{branch}"
    cache = _load_json(PR_CACHE, {})
    entry = cache.get(key) if isinstance(cache, dict) else None
    now = time.time()
    fresh = isinstance(entry, dict) and now - entry.get("checked_at", 0) <= PR_TTL
    refreshing = isinstance(entry, dict) and now - entry.get("refreshing_at", 0) <= 30
    if not fresh and not refreshing and not os.environ.get("QUOTA_PR_NO_REFRESH"):
        if not isinstance(cache, dict):
            cache = {}
        cache[key] = dict(entry or {}, refreshing_at=now)
        _write_pr_cache(cache)
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__),
                              "--refresh-pr", top, branch],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass
    return entry if isinstance(entry, dict) and entry.get("state") else None


def _write_pr_cache(cache):
    try:
        tmp = PR_CACHE + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PR_CACHE)
    except Exception:
        pass


def _refresh_pr(top, branch):
    state = None
    try:
        out = subprocess.run(["gh", "pr", "view", branch,
                              "--json", "state,statusCheckRollup,number,url"],
                             cwd=top, capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            d = json.loads(out.stdout)
            if d.get("state") == "OPEN":
                rolls = d.get("statusCheckRollup") or []
                good = ("SUCCESS", "NEUTRAL", "SKIPPED")
                green = bool(rolls) and all(
                    (r.get("conclusion") or r.get("state")) in good for r in rolls)
                state = {"state": "green" if green else "open",
                         "number": d.get("number"), "url": d.get("url")}
    except Exception:
        pass
    now = time.time()
    cache = _load_json(PR_CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    cache[f"{top}:{branch}"] = dict(state or {"state": None}, checked_at=now)
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and now - v.get("checked_at", 0) < 86400}
    _write_pr_cache(cache)
    return 0


def _git_segment(cwd, config):
    """`􀐞 repo 􀜞` on main, `􀐞 repo 􀣽 branch` on a branch; the state symbol
    encodes clean/modified and open/green PRs. Outside a git repo, the
    ~-shortened path."""
    if not cwd:
        return ""
    short = "~" + cwd[len(HOME):] if cwd.startswith(HOME) else cwd
    if not os.path.isdir(cwd):
        return short
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return short
    style = config.get("statusline_git_symbols", "auto")
    if style == "auto":
        style = "sf" if sys.platform == "darwin" else "ascii"
    sym = GIT_SYMBOLS_SF if style == "sf" else GIT_SYMBOLS_ASCII
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    dirty = bool(_git(cwd, "status", "--porcelain"))
    on_main = branch in ("main", "master")
    pr = None if on_main else _pr_state(top, branch)
    prstate = pr.get("state") if isinstance(pr, dict) else None
    if prstate == "green":
        state = sym["pr_green"]
    elif prstate == "open":
        state = sym["pr_open"]
    elif dirty:
        state = sym["main_dirty"] if on_main else sym["branch_dirty"]
    else:
        state = sym["main_clean"] if on_main else sym["branch_clean"]
    seg = f"{sym['repo']} {os.path.basename(top)} {state} {branch}"
    num = pr.get("number") if isinstance(pr, dict) else None
    if prstate and num:
        tag = f"#{num}"
        url = pr.get("url")
        if url:
            # OSC 8 hyperlink; terminals without support just show the text
            tag = f"\033]8;;{url}\033\\{tag}\033]8;;\033\\"
        seg += f" {tag}"
    return seg


def _theme_to_hex(theme):
    """A Claude Code `theme` setting of custom:<name> maps to
    ~/.claude/themes/<name>.json; overrides.claude is the accent slot."""
    if not (isinstance(theme, str) and theme.startswith("custom:")):
        return None
    t = _load_json(os.path.join(THEMES_DIR, theme.split(":", 1)[1] + ".json"), {})
    ov = t.get("overrides")
    if not isinstance(ov, dict):
        return None
    v = ov.get("claude") or ov.get("promptBorder")
    return v if isinstance(v, str) and v.startswith("#") else None


AGENTS = os.path.join(ROUTER_DIR, "agents.json")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
CODEX_ACTIVE_SECONDS = 120
AGENT_BADGE_BASE = 0x1000CB  # SF Symbols 1.square.fill, +2 per digit
EFFORT_LABELS = ("low", "medium", "high", "xhigh", "max")
GRAY, WHITE = "\033[38;5;247m", "\033[97m"
EFFORT_COLORS = {"low": "\033[33m",                    # yellow
                 "medium": "\033[32m",                 # green
                 "high": "\033[38;2;179;153;255m",     # light purple
                 "xhigh": "\033[38;2;124;58;237m"}     # purple
                                                       # max: rainbow


def _rainbow(text, now):
    """Per-character hue gradient; the phase rotates with `now`, so the text
    shimmers across statusline repaints. As animated as a statusline gets."""
    import colorsys
    out = []
    n = max(1, len(text))
    for i, ch in enumerate(text):
        hue = (now / 3.0 + i / n) % 1.0
        r, g, b = (int(v * 255) for v in colorsys.hsv_to_rgb(hue, 0.8, 1.0))
        out.append(f"\033[38;2;{r};{g};{b}m{ch}")
    return "".join(out) + RESET


def _effort_paint(text, effort, now, color_on):
    if not color_on:
        return text
    e = str(effort or "").lower()
    if e == "max":
        return _rainbow(text, now)
    code = EFFORT_COLORS.get(e)
    return f"{code}{text}{RESET}" if code else text


def _badge(n):
    if 1 <= n <= 9:
        return chr(AGENT_BADGE_BASE + 2 * (n - 1))
    return f"{n}."


def _codex_active(now):
    """Rollout files written in the last couple of minutes = live Codex runs.
    Model and effort come from the newest turn_context in the file tail."""
    out = []
    try:
        days = sorted(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*")))[-2:]
        for day in days:
            for p in glob.glob(os.path.join(day, "rollout-*.jsonl")):
                try:
                    if now - os.path.getmtime(p) > CODEX_ACTIVE_SECONDS:
                        continue
                    with open(p, "rb") as f:
                        f.seek(0, 2)
                        f.seek(max(0, f.tell() - 65536))
                        tail = f.read().decode("utf-8", "replace")
                    models = re.findall(r'"model":"([^"]+)"', tail)
                    efforts = re.findall(r'"effort":"([^"]+)"', tail)
                    out.append({"model": models[-1] if models else "codex",
                                "effort": efforts[-1] if efforts else None})
                except OSError:
                    continue
    except Exception:
        pass
    return out


def _agents_entries(cache, now, session_effort):
    """(model, effort) per running subagent — Claude registry first, then
    live Codex runs."""
    entries = []
    reg = _load_json(AGENTS, [])
    if isinstance(reg, list):
        for e in sorted(reg, key=lambda x: x.get("started_at", 0)):
            if not isinstance(e, dict):
                continue
            model = e.get("model")
            if not model:
                meta = cache.get("meta") if isinstance(cache.get("meta"), dict) else {}
                model = str(meta.get("model") or "claude").lower() \
                    .replace("claude-", "").replace(" ", "-")
            entries.append((str(model), session_effort))
    for c in _codex_active(now):
        entries.append((c["model"].split("-")[-1], c.get("effort")))
    return entries


def _agents_segment(cache, now, session_effort, color_on=False):
    """`􀃋 sonnet high  􀃍 sol xhigh` — gray numbered badge per running
    subagent, model + effort painted in the effort's color."""
    entries = _agents_entries(cache, now, session_effort)
    parts = []
    for i, (model, effort) in enumerate(entries[:9], 1):
        label = str(effort or "").lower()
        text = f"{model} {label}" if label in EFFORT_LABELS else model
        badge = f"{GRAY}{_badge(i)}{RESET}" if color_on else _badge(i)
        parts.append(badge + " " + _effort_paint(text, label, now, color_on))
    if len(entries) > 9:
        parts.append(f"+{len(entries) - 9}")
    return "  ".join(parts)


def _accent_hex(config, cwd):
    """Per-repo accent, nearest configuration wins: STATUSLINE_ACCENT env var,
    then — walking up from the session's cwd — an explicit `statusline_accent`
    key or the accent of a custom Claude Code theme in .claude settings, then
    the globally configured theme, then the router config, then coral."""
    env = os.environ.get("STATUSLINE_ACCENT")
    if env:
        return env
    d = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
    while d and d != "/":
        for name in ("settings.local.json", "settings.json"):
            s = _load_json(os.path.join(d, ".claude", name), {})
            v = s.get("statusline_accent")
            if isinstance(v, str) and v:
                return v
            t = _theme_to_hex(s.get("theme"))
            if t:
                return t
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    t = _theme_to_hex(
        _load_json(os.path.join(HOME, ".claude", "settings.json"), {}).get("theme"))
    if t:
        return t
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
    place = _git_segment(cwd_raw, config)

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
    if place:
        parts.append(c(accent, place))
    head_painted = _effort_paint(head, effort, now, color_on)
    if constrained:
        parts.append(head_painted + " " + pct_str(cl) if cl else head_painted)
        parts.append(c(GRAY, "codex") + " " + (pct_str(cx) if cx else "--"))
    else:
        parts.append(head_painted)
    agents = _agents_segment(cache, now, effort, color_on)
    if agents:
        parts.append(agents)
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
        global _effort, AGENTS, _codex_active
        saved_eff, saved_agents, saved_cact = _effort, AGENTS, _codex_active
        _effort = lambda: "high"
        AGENTS = os.path.join(d, "agents.json")  # not created yet: no agents
        _codex_active = lambda now: []
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
            os.environ.pop("STATUSLINE_ACCENT", None)

            # custom Claude Code theme provides the accent
            global THEMES_DIR
            saved_themes = THEMES_DIR
            THEMES_DIR = os.path.join(d, "themes")
            os.makedirs(THEMES_DIR)
            with open(os.path.join(THEMES_DIR, "x.json"), "w") as f:
                json.dump({"name": "X", "base": "dark",
                           "overrides": {"claude": "#4cb782"}}, f)
            proj2 = os.path.join(d, "proj2")
            os.makedirs(os.path.join(proj2, ".claude"))
            sl_path = os.path.join(proj2, ".claude", "settings.local.json")
            with open(sl_path, "w") as f:
                json.dump({"theme": "custom:x"}, f)
            try:
                check("accent_from_theme", _accent_hex({}, proj2) == "#4cb782")
                with open(sl_path, "w") as f:
                    json.dump({"theme": "custom:x",
                               "statusline_accent": "#111111"}, f)
                check("accent_key_beats_theme", _accent_hex({}, proj2) == "#111111")
            finally:
                THEMES_DIR = saved_themes
        finally:
            os.environ.pop("STATUSLINE_ACCENT", None)
            if saved_env:
                os.environ["STATUSLINE_ACCENT"] = saved_env

        # ---- git segment ----
        global PR_CACHE, _pr_state
        saved_prc, saved_prs = PR_CACHE, _pr_state
        PR_CACHE = os.path.join(d, "pr-cache.json")
        os.environ["QUOTA_PR_NO_REFRESH"] = "1"
        cfgg = {"statusline_git_symbols": "sf"}
        repo = os.path.join(d, "myrepo")
        os.makedirs(repo)
        import subprocess as sp

        def git(*a):
            sp.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t"]
                   + list(a), capture_output=True)

        git("init", "-q", "-b", "main")
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("x")
        git("add", "a.txt")
        git("commit", "-qm", "init")
        try:
            check("git_main_clean", _git_segment(repo, cfgg) == "􀐞 myrepo 􀜞 main")
            with open(os.path.join(repo, "a.txt"), "w") as f:
                f.write("y")
            check("git_main_dirty", _git_segment(repo, cfgg) == "􀐞 myrepo 􀧙 main")
            git("checkout", "-qb", "feat")
            check("git_branch_dirty", _git_segment(repo, cfgg) == "􀐞 myrepo 􀫲 feat")
            git("commit", "-aqm", "wip")
            check("git_branch_clean", _git_segment(repo, cfgg) == "􀐞 myrepo 􀣽 feat")
            _pr_state = lambda t, b: {"state": "open", "number": 4, "url": None}
            check("git_pr_open", _git_segment(repo, cfgg) == "􀐞 myrepo 􀩄 feat #4")
            _pr_state = lambda t, b: {"state": "green", "number": 4,
                                      "url": "https://github.com/x/y/pull/4"}
            seg = _git_segment(repo, cfgg)
            check("git_pr_green_linked", seg.startswith("􀐞 myrepo 􀁣 feat ")
                  and "\033]8;;https://github.com/x/y/pull/4\033\\#4\033]8;;\033\\" in seg)
            nongit = os.path.join(d, "plain")
            os.makedirs(nongit)
            check("git_fallback_path", _git_segment(nongit, cfgg) == nongit)
        finally:
            PR_CACHE, _pr_state = saved_prc, saved_prs
            os.environ.pop("QUOTA_PR_NO_REFRESH", None)

        # ---- running agents segment ----
        check("badge_formula", _badge(1) == "􀃋" and _badge(2) == "􀃍"
              and _badge(3) == "􀃏" and _badge(12) == "12.")
        with open(AGENTS, "w") as f:
            json.dump([{"id": "a", "session_id": "s", "model": "sonnet",
                        "background": True, "started_at": now},
                       {"id": "b", "session_id": "s", "model": None,
                        "background": True, "started_at": now + 1}], f)
        _codex_active = lambda now: [{"model": "gpt-5.6-sol", "effort": "xhigh"}]
        cache_meta = {"meta": {"model": "Fable 5"}}
        seg = _agents_segment(cache_meta, now, "high")
        check("agents_segment",
              seg == "􀃋 sonnet high  􀃍 fable-5 high  􀃏 sol xhigh")
        seg2 = _agents_segment(cache_meta, now, "low")
        check("agents_effort_labels", "sonnet low" in seg2 and "sol xhigh" in seg2)
        _load_json_from_probe = lambda: {"available": False}
        try:
            la = _render(low, cfgm, now, payload)
        finally:
            _load_json_from_probe = saved
        check("agents_in_render", "􀃋" in la and "sonnet" in la and "sol" in la)

        # effort color palette on agents; gray badges
        seg_c = _agents_segment(cache_meta, now, "high", True)
        check("agents_effort_colors",
              EFFORT_COLORS["high"] + "sonnet high" in seg_c
              and EFFORT_COLORS["xhigh"] + "sol xhigh" in seg_c
              and GRAY + "􀃋" in seg_c)

        # rainbow max: truecolor per char, shimmers with time, off when uncolored
        r1 = _effort_paint("fable-5 max", "max", 100, True)
        r2 = _effort_paint("fable-5 max", "max", 101, True)
        check("rainbow_max", "\033[38;2;" in r1 and r1 != r2
              and _effort_paint("x", "max", 5, False) == "x")

        # order: place · model · agents
        check("minimal_order",
              la.index("~/projects") < la.index("opus-4.8") < la.index("􀃋"))
        os.unlink(AGENTS)
        _codex_active = lambda now: []
        _effort, AGENTS, _codex_active = saved_eff, saved_agents, saved_cact

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
    if len(sys.argv) > 3 and sys.argv[1] == "--refresh-pr":
        sys.exit(_refresh_pr(sys.argv[2], sys.argv[3]))
    sys.exit(main())
