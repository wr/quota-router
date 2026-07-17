# AGENTS.md

Guidance for coding agents (Claude Code, Codex) working in this repository.

## Source of truth
- GitHub: github.com/wr/quota-router
- Linear project: Personal team, no specific project
- Branch prefix: wells/
- PR mode: ready

## What this is

Quota-aware routing between Claude Code and OpenAI Codex: a status line, a
PreToolUse hook that injects live quota at every subagent launch, a routing
policy skill, and an opt-in hibernate/auto-resume pair for surviving the
5-hour cap. ~2,500 lines of stdlib-only Python 3 — no dependencies, no build
system, no package manager. `docs/HOW-IT-WORKS.md` is the full design doc,
including the config reference and known limitations.

## Commands

```sh
# The test suite is per-script self-tests (this is what CI runs):
python3 router/codex_quota_probe.py --self-test
python3 router/statusline.py --self-test
python3 router/pretooluse_hook.py --self-test
python3 router/hibernate_hook.py --self-test
python3 router/hibernate_watchdog.py --self-test

# Hook smoke test (also in CI) — a complete-payload variant lives in .github/workflows/test.yml:
echo '{"hook_event_name":"PreToolUse","tool_name":"Agent"}' | python3 router/pretooluse_hook.py

# Render every statusline state in the terminal:
python3 router/statusline.py --demo

# Regenerate the README image (needs vhs):
cd docs/demo && vhs demo.tape   # writes docs/demo.png via demo.py

# Deploy repo changes to the live install (idempotent, preserves config.json):
./install.sh
# Test an install without touching the real one:
CLAUDE_DIR=/tmp/fake-claude ./install.sh
```

There is no lint config; match the existing style (stdlib only, no
f-string gymnastics, functions over classes).

## The repo is live in this machine's Claude Code

`install.sh` copies `router/*.py` to `~/.claude/quota-router/` and
`skill/SKILL.md` to `~/.claude/skills/subagent-router/`. **Editing repo files
does nothing until `./install.sh` re-syncs them** — and re-syncing changes
the hooks, status line, and routing skill of the very session you're working
in. `~/.claude/quota-router/config.json` is user state and is never
overwritten by re-runs.

## Architecture

Data flow (full diagram in docs/HOW-IT-WORKS.md):

- `router/statusline.py` — Claude Code pipes status JSON (incl. `rate_limits`)
  to it every tick. It caches those numbers to `~/.claude/quota-cache.json`
  and renders the readout (styles: minimal/braille/circles/plain). Wraps any
  pre-existing status line recorded in config as `previous_statusline`.
- `router/codex_quota_probe.py` — reads the newest `token_count` event from
  `~/.codex/sessions/*.jsonl` rollout logs. Strictly read-only; refuses to
  guess past a window reset (`routing_available: false`).
- `router/pretooluse_hook.py` — one script, four hook events (PreToolUse /
  PostToolUse on Agent, SubagentStop, SessionEnd). Injects the quota snapshot
  as context at every Agent launch, and tracks running subagents for the
  status line (registered on launch, cleaned up on stop, 2-hour TTL for
  crash leftovers).
- `router/hibernate_hook.py` + `router/hibernate_watchdog.py` — Stop/
  Notification hook detects a usage-limit stop (cache at/above
  `hibernate_arm_pct` with a future reset — the limit banner never reaches
  hooks), writes a marker, spawns the detached watchdog, which resumes the
  session at reset via the original tmux pane or `claude --resume`.
  `statusline.py` arms the same marker from its own tick, because a cap
  crossed while a session sits idle (e.g. a limit-rejected `/loop` wakeup)
  emits no hook events at all.
- `skill/SKILL.md` — the routing policy. Classifies the task, gates each
  quota window separately, returns provider/model/effort/fan-out.

## Invariants (violating these has already caused shipped bugs)

- **Fail toward "unknown", never crash.** These scripts run inside the status
  line and hooks; an exception blanks or breaks the user's session. Unknown
  renders as `--`, stale as `~`.
- **Unknown is not 0%.** A missing/stale/rolled-over snapshot means "don't
  route on this", never "free headroom".
- **Never collapse the 5-hour and 7-day windows** into one number (e.g. with
  `max()`); they get separate gates and different policies.
- **A tick may only describe the current window.** Idle instances repaint
  with rate_limits from their last API response, so the cache drops incoming
  windows whose reset is already past and used% regressions within one
  window (`observed_at` is stamped by the reader — it proves nothing).
- **Both input formats are undocumented** (Claude's status payload, Codex's
  rollout logs) — parse defensively, tolerate missing keys, and keep
  `hibernate_hook.py`'s log-everything behavior so format changes are
  diagnosable.
- The model tier tables in `skill/SKILL.md` name plan-specific models on
  purpose; the gate logic must not depend on tier names.

## Conventions

- Every script keeps a `--self-test` mode; new behavior gets a case there
  (CI runs nothing else). Self-tests must not touch real state — they build
  temp fixtures.
- `install.sh`/`uninstall.sh` are transactional about `settings.json`:
  timestamped backup, tmp-file + `os.replace`, idempotent re-runs. Keep that
  property when adding hooks.
- README and docs are written in a personal, plain voice — keep additions
  consistent with it.
