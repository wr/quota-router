#!/usr/bin/env bash
set -euo pipefail

# quota-router installer.
#
# Copies the probes into ~/.claude/quota-router, installs the subagent-router
# skill, and registers the statusLine + PreToolUse hook in ~/.claude/settings.json
# (with a timestamped backup). Idempotent: safe to re-run. An existing
# config.json is never overwritten; an existing (non-quota-router) status line
# is recorded in config.json and wrapped, not replaced.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
ROUTER_DIR="$CLAUDE_DIR/quota-router"
SKILL_DIR="$CLAUDE_DIR/skills/subagent-router"
SETTINGS="$CLAUDE_DIR/settings.json"

command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }
[ -d "$CLAUDE_DIR" ] || { echo "error: $CLAUDE_DIR not found — is Claude Code installed?" >&2; exit 1; }

mkdir -p "$ROUTER_DIR" "$SKILL_DIR"
chmod 700 "$ROUTER_DIR"

install -m 700 "$REPO_DIR/router/codex_quota_probe.py" "$ROUTER_DIR/"
install -m 700 "$REPO_DIR/router/statusline.py" "$ROUTER_DIR/"
install -m 700 "$REPO_DIR/router/pretooluse_hook.py" "$ROUTER_DIR/"
install -m 700 "$REPO_DIR/router/hibernate_hook.py" "$ROUTER_DIR/"
install -m 700 "$REPO_DIR/router/hibernate_watchdog.py" "$ROUTER_DIR/"
install -m 644 "$REPO_DIR/skill/SKILL.md" "$SKILL_DIR/SKILL.md"

if [ ! -f "$ROUTER_DIR/config.json" ]; then
  install -m 600 "$REPO_DIR/router/config.example.json" "$ROUTER_DIR/config.json"
  echo "created $ROUTER_DIR/config.json"
else
  echo "kept existing $ROUTER_DIR/config.json"
fi

python3 - "$SETTINGS" "$ROUTER_DIR" <<'PY'
import json, os, sys, time

settings_path, router_dir = sys.argv[1], sys.argv[2]
try:
    with open(settings_path) as f:
        settings = json.load(f)
except FileNotFoundError:
    settings = {}

if os.path.exists(settings_path):
    backup = f"{settings_path}.bak-{int(time.time())}"
    with open(backup, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"settings backup: {backup}")

MARK = "quota-router/statusline.py"
statusline_cmd = f'python3 "{router_dir}/statusline.py"'

# Record a pre-existing status line so statusline.py wraps it (and uninstall
# can restore it). Never record our own command as "previous".
prev = settings.get("statusLine")
if isinstance(prev, dict) and MARK not in str(prev.get("command", "")):
    cfg_path = os.path.join(router_dir, "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    if not cfg.get("previous_statusline"):
        cfg["previous_statusline"] = prev
        tmp = cfg_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg_path)
        print("recorded previous statusLine; it will be wrapped, not replaced")

settings["statusLine"] = {"type": "command", "command": statusline_cmd,
                          "refreshInterval": 30}

hooks = settings.setdefault("hooks", {})

def ensure_hook(event, script, matcher=None):
    arr = hooks.setdefault(event, [])
    if any(f"quota-router/{script}" in json.dumps(e) for e in arr):
        print(f"{event} hook already registered")
        return
    entry = {"hooks": [{"type": "command",
                        "command": f'python3 "{router_dir}/{script}"', "timeout": 10}]}
    if matcher:
        entry["matcher"] = matcher
    arr.append(entry)
    print(f"registered {event} hook" + (f" (matcher: {matcher})" if matcher else ""))

ensure_hook("PreToolUse", "pretooluse_hook.py", matcher="Agent")
ensure_hook("PostToolUse", "pretooluse_hook.py", matcher="Agent")
ensure_hook("SubagentStop", "pretooluse_hook.py")
ensure_hook("SessionEnd", "pretooluse_hook.py")
ensure_hook("Stop", "hibernate_hook.py")
ensure_hook("Notification", "hibernate_hook.py")

tmp = settings_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
os.replace(tmp, settings_path)
print("registered statusLine")
PY

echo
python3 "$ROUTER_DIR/codex_quota_probe.py" --self-test >/dev/null && echo "probe self-test: OK"
python3 "$ROUTER_DIR/statusline.py" --self-test >/dev/null && echo "statusline self-test: OK"
python3 "$ROUTER_DIR/hibernate_hook.py" --self-test >/dev/null && echo "hibernate hook self-test: OK"
python3 "$ROUTER_DIR/hibernate_watchdog.py" --self-test >/dev/null && echo "hibernate watchdog self-test: OK"
echo '{"hook_event_name":"PreToolUse","tool_name":"Agent"}' | python3 "$ROUTER_DIR/pretooluse_hook.py" \
  | python3 -c "import json,sys; json.load(sys.stdin)" >/dev/null && echo "hook smoke test: OK"

echo
echo "Installed. Restart Claude Code (or open /hooks once) to activate."
echo "The Claude side of the readout stays '--' until the first API response"
echo "of a fresh session populates the cache — that's normal."
