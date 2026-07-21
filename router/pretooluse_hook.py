#!/usr/bin/env python3
"""Agent-delegation hooks: quota context injection + running-agent registry.

PreToolUse (Agent): injects the live quota snapshot as additionalContext so
the routing gate is UNCONDITIONALLY present at the moment of delegation, and
records the launch in agents.json so the statusline can show running
subagents. Advisory: it never blocks the tool.

PostToolUse (Agent): removes foreground launches from the registry (for a
backgrounded agent the tool returns immediately, so its Post event is just
the "started" ack and the entry is kept).

SubagentStop: removes the oldest entry for the session — background agents'
best completion signal. SessionEnd: clears the session's entries. A 2-hour
TTL scrubs anything a crash leaves behind.

Always exits 0 — a hook must never wedge a delegation.
"""
import fcntl
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
AGENTS = os.path.join(ROUTER, "agents.json")
AGENTS_LOCK = AGENTS + ".lock"
AGENT_TTL = 2 * 3600


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# running-agent registry
# ---------------------------------------------------------------------------
def _update_agents(mutate):
    fd = os.open(AGENTS_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        reg = _load(AGENTS, [])
        if not isinstance(reg, list):
            reg = []
        now = time.time()
        reg = [e for e in reg if isinstance(e, dict)
               and now - e.get("started_at", 0) < AGENT_TTL
               and (e.get("id") or e.get("session_id"))]
        reg = mutate(reg, now)
        tmp = AGENTS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(reg, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, AGENTS)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _register(evt):
    ti = evt.get("tool_input") or {}
    entry = {"id": evt.get("tool_use_id"),
             "session_id": evt.get("session_id"),
             "model": ti.get("model"),
             "type": ti.get("subagent_type"),
             # the Agent tool backgrounds by default
             "background": bool(ti.get("run_in_background", True)),
             "started_at": time.time()}
    _update_agents(lambda reg, now: reg + [entry])


def _deregister(evt, scope):
    """scope: 'foreground' (Post: by id, only non-background entries),
    'oldest' (SubagentStop, which carries no tool_use_id), 'session'
    (SessionEnd)."""
    tid = evt.get("tool_use_id")
    sid = evt.get("session_id")

    def mut(reg, now):
        if scope == "session":
            return [e for e in reg if e.get("session_id") != sid]
        if scope == "foreground":
            hit = [e for e in reg if tid and e.get("id") == tid]
            if hit and not hit[0].get("background"):
                return [e for e in reg if e.get("id") != tid]
            return reg
        # strictly this session's entries: the registry is shared across
        # instances, and falling back to the whole list would delete another
        # session's agent (the TTL handles orphans instead)
        mine = [e for e in reg if e.get("session_id") == sid]
        if mine:
            oldest = min(mine, key=lambda e: e.get("started_at", 0))
            return [e for e in reg if e is not oldest]
        return reg

    _update_agents(mut)


# ---------------------------------------------------------------------------
# quota context (PreToolUse)
# ---------------------------------------------------------------------------
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


def _fable_fraction(cfg):
    """fable_weekly_fraction must be a number in (0, 1]; anything else
    (missing, wrong type, out of range) falls back to the 0.5 default."""
    v = cfg.get("fable_weekly_fraction", 0.5)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 1):
        return 0.5
    return v


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

    # Fable's weekly sub-limit isn't in the status payload (only five_hour/
    # seven_day are) — estimate a worst-case upper bound from the 7d window.
    # Non-Fable usage inflates this estimate, which is the safe direction for
    # a protect gate; unknown when the 7d window isn't fresh, never a number.
    fable_est = None
    fable_fraction = _fable_fraction(cfg)
    if cfg.get("fable_available_on_plan") and seven and seven["fresh"]:
        fable_est = min(100, round(seven["used"] / fable_fraction, 1))
        lines.append(f"  Fable wk: ≤{fable_est}% (est: 7d ÷ {fable_fraction} sub-limit; "
                     f"not directly readable)")

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
    conserve = cfg.get("fivehour_conserve_pct", 50)
    if five and five["fresh"] and five["used"] > five_thr:
        soon = five["minutes_to_reset"] is not None and five["minutes_to_reset"] <= 20
        if soon:
            signals.append(f"5h SOFT (>{five_thr}%) but resets soon: prefer waiting or a lower Claude tier over offloading.")
        else:
            signals.append(f"5h CONSTRAINED (>{five_thr}%): drop a Claude tier first, else offload to Codex.")
    elif five and five["fresh"] and five["used"] >= conserve:
        signals.append(f"5h CONSERVE (≥{conserve}%): for standard/mechanical work prefer a "
                       f"Codex agent (or Haiku) over mid/high Claude tiers — save the Claude "
                       f"window for what needs Claude.")
    fable_protect_pct = cfg.get("fable_weekly_protect_pct", 80)
    if fable_est is not None and fable_est > fable_protect_pct:
        signals.append(f"FABLE WEEKLY BINDING (est >{fable_protect_pct}%): no Fable-tier subagent "
                       f"launches; keep orchestrator turns minimal and delegate to pinned cheaper models.")
    meta = cache.get("meta") if isinstance(cache.get("meta"), dict) else {}
    if "fable" in str(meta.get("model") or "").lower():
        signals.append("ORCHESTRATOR IS FABLE: pin an explicit cheaper `model` on every Agent call — "
                       "subagents inherit the parent model and spend the Fable sub-limit otherwise. "
                       "Delegate execution regardless of quota headroom; quota picks the target, "
                       "never whether.")
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
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        evt = json.loads(raw) if raw.strip() else {}
    except Exception:
        evt = {}
    name = evt.get("hook_event_name")

    if name == "PostToolUse":
        try:
            if evt.get("tool_name") == "Agent":
                _deregister(evt, "foreground")
        except Exception:
            pass
        return 0
    if name == "SubagentStop":
        try:
            _deregister(evt, "oldest")
        except Exception:
            pass
        return 0
    if name == "SessionEnd":
        try:
            _deregister(evt, "session")
        except Exception:
            pass
        return 0

    if name != "PreToolUse":
        # unknown/unparseable event: never fall through to registration
        return 0

    # PreToolUse (matcher: Agent)
    try:
        if evt.get("tool_name") == "Agent" and evt.get("tool_use_id"):
            _register(evt)
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


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test():
    global AGENTS, AGENTS_LOCK
    import tempfile
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        print(("PASS " if cond else "FAIL ") + name)
        if cond:
            passed += 1
        else:
            failed += 1

    def reg():
        return _load(AGENTS, [])

    with tempfile.TemporaryDirectory() as d:
        AGENTS = os.path.join(d, "agents.json")
        AGENTS_LOCK = AGENTS + ".lock"

        # 1. register foreground + background (background is the default)
        _register({"tool_use_id": "t1", "session_id": "s1",
                   "tool_input": {"model": "sonnet", "subagent_type": "Explore",
                                  "run_in_background": False}})
        _register({"tool_use_id": "t2", "session_id": "s1",
                   "tool_input": {"subagent_type": "general-purpose"}})
        check("registered_two", len(reg()) == 2)
        check("bg_default_true",
              [e for e in reg() if e["id"] == "t2"][0]["background"] is True)

        # 2. Post removes a foreground entry by id
        _deregister({"tool_use_id": "t1", "session_id": "s1"}, "foreground")
        check("post_removes_foreground", [e["id"] for e in reg()] == ["t2"])

        # 3. Post does NOT remove a background entry (its Post is the start ack)
        _deregister({"tool_use_id": "t2", "session_id": "s1"}, "foreground")
        check("post_keeps_background", [e["id"] for e in reg()] == ["t2"])

        # 4. SubagentStop removes the oldest entry for the session
        _register({"tool_use_id": "t3", "session_id": "s1",
                   "tool_input": {"model": "haiku"}})
        _deregister({"session_id": "s1"}, "oldest")
        check("subagentstop_removes_oldest", [e["id"] for e in reg()] == ["t3"])

        # 4b. SubagentStop for a session with no entries must not touch
        # other sessions' entries (multi-instance safety)
        _deregister({"session_id": "other-instance"}, "oldest")
        check("subagentstop_scoped_to_session", [e["id"] for e in reg()] == ["t3"])

        # 5. SessionEnd clears the session
        _register({"tool_use_id": "t4", "session_id": "s2", "tool_input": {}})
        _deregister({"session_id": "s1"}, "session")
        check("sessionend_clears_session", [e["id"] for e in reg()] == ["t4"])

        # 6. TTL scrub drops stale entries on the next mutation
        stale = {"id": "old", "session_id": "s3",
                 "started_at": time.time() - AGENT_TTL - 60, "background": True}
        current = reg()
        with open(AGENTS, "w") as f:
            json.dump(current + [stale], f)
        _update_agents(lambda r, now: r)
        check("ttl_scrub", [e["id"] for e in reg()] == ["t4"])

        # 7. phantom entries (no id, no session) are scrubbed
        current = reg()
        with open(AGENTS, "w") as f:
            json.dump(current + [{"id": None, "session_id": None,
                                  "background": True,
                                  "started_at": time.time()}], f)
        _update_agents(lambda r, now: r)
        check("phantom_scrubbed", [e["id"] for e in reg()] == ["t4"])

        # ---- Fable weekly estimate (build_context) ----
        global CACHE, CONFIG, _codex
        saved_cache, saved_config, saved_codex = CACHE, CONFIG, _codex
        CACHE = os.path.join(d, "quota-cache.json")
        CONFIG = os.path.join(d, "config.json")
        _codex = lambda: {"available": False}

        def write_cache(seven_used, seven_fresh=True, meta_model=None, missing=False):
            fnow = time.time()
            data = {}
            if not missing:
                data["seven_day"] = {
                    "used_percentage": seven_used,
                    "resets_at": fnow + 500000,
                    "observed_at": fnow if seven_fresh else fnow - 10000,
                    "present_in_latest_payload": seven_fresh,
                }
            if meta_model:
                data["meta"] = {"model": meta_model}
            with open(CACHE, "w") as f:
                json.dump(data, f)

        def write_config(**kw):
            base = {"fable_available_on_plan": True}
            base.update(kw)
            with open(CONFIG, "w") as f:
                json.dump(base, f)

        try:
            # correct x2 math with the default fraction
            write_cache(47)
            write_config()
            ctx = build_context()
            check("fable_estimate_math", "Fable wk: ≤94.0%" in ctx)

            # capped at 100
            write_cache(60)
            ctx = build_context()
            check("fable_estimate_capped", "Fable wk: ≤100" in ctx)

            # absent when the 7d window is stale
            write_cache(47, seven_fresh=False)
            ctx = build_context()
            check("fable_absent_when_stale", "Fable wk" not in ctx)

            # absent when the 7d window is missing entirely
            write_cache(47, missing=True)
            ctx = build_context()
            check("fable_absent_when_missing", "Fable wk" not in ctx)

            # absent when the plan flag is false
            write_cache(47)
            write_config(fable_available_on_plan=False)
            ctx = build_context()
            check("fable_absent_when_flag_false", "Fable wk" not in ctx)

            # binding signal fires above the threshold, not at/below it
            write_cache(47)  # -> 94% > 80% default threshold
            write_config()
            ctx = build_context()
            check("fable_binding_fires_above", "FABLE WEEKLY BINDING" in ctx)

            write_cache(40)  # -> 80%, not > 80% threshold
            write_config()
            ctx = build_context()
            check("fable_binding_not_at_threshold", "FABLE WEEKLY BINDING" not in ctx)

            # orchestrator-pin signal keys off cache.meta.model, not the estimate
            write_cache(10, meta_model="Fable 5")
            write_config()
            ctx = build_context()
            check("fable_orchestrator_pin_fires", "ORCHESTRATOR IS FABLE" in ctx)

            write_cache(10, meta_model="Opus 4.8")
            ctx = build_context()
            check("fable_orchestrator_pin_absent_opus", "ORCHESTRATOR IS FABLE" not in ctx)

            # bad fraction values all fall back to the 0.5 default
            for bad in (0, -1, 2, "x"):
                write_cache(47)
                write_config(fable_weekly_fraction=bad)
                ctx = build_context()
                check(f"fable_bad_fraction_{bad}", "Fable wk: ≤94.0%" in ctx)
        finally:
            CACHE, CONFIG, _codex = saved_cache, saved_config, saved_codex

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_self_test())
    sys.exit(main())
