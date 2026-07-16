#!/usr/bin/env bash
set -euo pipefail

# quota-router uninstaller.
#
# Removes the statusLine (restoring a wrapped previous one, if any) and the
# PreToolUse hook from ~/.claude/settings.json, then deletes the router files,
# the skill, and the quota cache. Pass -y to skip the confirmation.

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
ROUTER_DIR="$CLAUDE_DIR/quota-router"
SKILL_DIR="$CLAUDE_DIR/skills/subagent-router"
SETTINGS="$CLAUDE_DIR/settings.json"

if [ "${1:-}" != "-y" ]; then
  read -r -p "Remove quota-router (files, skill, settings entries)? [y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "aborted"; exit 1 ;; esac
fi

python3 - "$SETTINGS" "$ROUTER_DIR" <<'PY'
import json, os, sys, time

settings_path, router_dir = sys.argv[1], sys.argv[2]
try:
    with open(settings_path) as f:
        settings = json.load(f)
except FileNotFoundError:
    sys.exit(0)

backup = f"{settings_path}.bak-{int(time.time())}"
with open(backup, "w") as f:
    json.dump(settings, f, indent=2)
print(f"settings backup: {backup}")

sl = settings.get("statusLine")
if isinstance(sl, dict) and "quota-router/statusline.py" in str(sl.get("command", "")):
    prev = None
    try:
        with open(os.path.join(router_dir, "config.json")) as f:
            prev = json.load(f).get("previous_statusline")
    except Exception:
        pass
    if prev:
        settings["statusLine"] = prev
        print("restored previous statusLine")
    else:
        settings.pop("statusLine", None)
        print("removed statusLine")

for event in ("PreToolUse", "Stop", "Notification"):
    arr = settings.get("hooks", {}).get(event)
    if isinstance(arr, list):
        kept = [e for e in arr if "quota-router/" not in json.dumps(e)]
        if len(kept) != len(arr):
            settings["hooks"][event] = kept
            print(f"removed {event} hook")
        if not settings["hooks"].get(event):
            settings["hooks"].pop(event, None)

tmp = settings_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
os.replace(tmp, settings_path)
PY

rm -rf "$ROUTER_DIR" "$SKILL_DIR"
rm -f "$CLAUDE_DIR/quota-cache.json" "$CLAUDE_DIR/quota-cache.lock"
echo "Removed $ROUTER_DIR, $SKILL_DIR, and the quota cache."
echo "Restart Claude Code to drop the old hook registration."
