# How it works

The README covers what you get; this covers how, and where the sharp edges
are.

## Why this needs to exist

**Every Claude subagent draws from the same pool.** A three-way parallel
fan-out isn't extra capacity — it's your 5-hour window draining three times
faster. The only lever that adds real capacity is the other provider.

**The weekly window is the one that hurts.** The 5-hour window heals itself
while you eat lunch. Hit the 7-day cap on a Wednesday and you're rationing
until reset. The two windows need different policies, and most people (and
models) treat them as one number.

**The model making delegation decisions can't see quota.** Claude will happily
fan out five Opus subagents at 88% weekly usage, because nothing told it. A
skill it might remember to consult isn't enough; the numbers have to show up
unprompted, at decision time. That's what the hook is for.

## The pieces

```mermaid
flowchart LR
    CC[Claude Code] -- "status JSON, every tick" --> SL[statusline.py]
    SL --> CACHE[("~/.claude/quota-cache.json")]
    LOGS[("~/.codex/sessions/*.jsonl")] --> PROBE[codex_quota_probe.py]
    PROBE --> SL
    CACHE --> HOOK[pretooluse_hook.py]
    PROBE --> HOOK
    HOOK -- "injected on every Agent call" --> DECIDE{{"routing decision — SKILL.md"}}
    CC -- "Stop / Notification events" --> HIB[hibernate_hook.py]
    HIB -- "usage-limit stop detected" --> WD[hibernate_watchdog.py]
    WD -- "window reset → resume session" --> CC
```

| File | Role |
|---|---|
| `router/statusline.py` | Claude Code pipes status JSON (including `rate_limits`) to the status-line script on every tick. This caches those numbers and renders the readout. If you already had a status line, it gets wrapped, not replaced. |
| `router/codex_quota_probe.py` | Reads the newest `token_count` event from Codex's rollout logs. Read-only: never launches Codex, never spends a token. |
| `router/pretooluse_hook.py` | A `PreToolUse` hook on `Agent` calls. Injects the live snapshot as context every time a subagent is about to launch, whether or not anyone remembered to check. |
| `router/hibernate_hook.py` | Stop/Notification hook. Detects a usage-limit stop (and logs every event it sees, since the exact cap-event shape is undocumented), then arms the watchdog. |
| `router/hibernate_watchdog.py` | Waits out the window reset, then resumes the interrupted session — by typing into the original tmux pane, or `claude --resume` as a fallback. |
| `skill/SKILL.md` | The policy. Classify the task, gate each window on quota, pick provider, model, effort, and fan-out. |

Every script has a `--self-test`; CI runs them all.

## Reading the readout

The default style is `minimal` — quota is invisible until it matters:

```
opus-4.8 high · ~/projects                                  (all quiet)
opus-4.8 high 76% (2h10m) · codex 82% (2h42m) · ~/projects  (constrained)
```

- Model + effort in the accent color; the place segment in gray shows the
  repo and its git state: `􀐞 repo 􀜞` (repo icon, then a state symbol —
  main/clean, main/modified `􀧙`, branch/clean `􀣽` + branch name,
  branch/modified `􀫲`, PR in review `􀩄`, PR checks green `􀁣`). When a PR
  exists, the branch name is followed by `#N` — an OSC 8 hyperlink to the PR
  in terminals that support it. PR state is cached for 3 minutes and
  refreshed by a detached background `gh` call, so ticks never wait on the
  network. Outside a git repo it's the ~-shortened
  path. The symbols are SF Symbols (macOS); on other platforms — or with
  `statusline_git_symbols: "ascii"` — a plain unicode set is used.
- Below `statusline_show_pct` (default 75) on both providers: no numbers at
  all. The moment either crosses it, both providers' most-pressured windows
  appear — your usage and the alternative, in one glance.
- Percentages go yellow at the show threshold, red + bold past the window's
  own gate (85% for 5-hour windows, 75% for weekly).
- The countdown appears when the reset is near enough to be actionable
  (within 8 hours) — a weekly window days from reset shows just the number.
- `~` means the number is stale; `--` means unknown.
- A leading `⏾` means a capped session is hibernating.
- Running subagents appear as numbered badges with model and effort:
  `􀃋 sonnet high  􀃍 sol xhigh`. Claude launches are tracked by the Agent-tool
  hooks (PreToolUse registers, PostToolUse/SubagentStop/SessionEnd clean up,
  a 2-hour TTL scrubs crash leftovers); Codex runs are detected from rollout
  files written in the last two minutes, with model and effort read from the
  file tail. Claude agents that don't set an explicit model show the session
  model; their effort label is the session effort (agent definitions can
  override effort invisibly). Codex entries linger up to ~2 minutes after a
  run finishes.

The always-on gauge styles (`statusline_style: braille`, `circles`, or
`plain`) show every window all the time instead:

```
Claude 5h ⣿88%!(34m) / 7d ⣧71% · Codex wk ⡄19%
```

Run `statusline.py --demo` to see the styles rendered in your own terminal.

## The routing policy, in short

The full decision procedure lives in [`skill/SKILL.md`](../skill/SKILL.md).
The load-bearing ideas:

- **Quota is a hard gate; model strength only breaks ties.**
- **Windows are gated separately**, never collapsed into one number. Weekly
  above 75%: protect hard — offload to Codex, drop the frontier tier,
  fan-out 1. The 5-hour window above 85% (counting a per-tier launch
  reserve): drop a tier, wait if the reset is minutes away, otherwise
  offload.
- **Unknown is not 0%.** A stale snapshot or a rolled-over window means
  "don't route on this", never "free headroom".
- **Reset proximity decides wait-vs-switch.** It never discounts the capacity
  check itself.
- **Adversarial review crosses providers.** Whichever model wrote the thing
  doesn't get to review it.
- **On a 429**, mark that provider binding, retry once on the other at the
  same or cheaper tier, then stop. No retry loops, no post-429 fan-out.

## Hibernate: surviving the 5-hour cap

Claude Code has no native "wait for the limit to reset and keep going" — when
the window caps out, the session stops and waits for you. With
`hibernate_enabled: true`, quota-router babysits instead:

1. A Stop/Notification hook spots the cap. The reliable trigger is the quota
   cache: Claude Code's limit banner never reaches hooks (verified in the
   field), but the cache reads 100% at cap — so any Stop/Notification while a
   window sits at/above `hibernate_arm_pct` (default 99.5) with a future
   reset arms hibernation. A limit-looking message in the event text also
   arms, when one ever shows up. Either way the cache must confirm real
   pressure, so a session merely *talking about* limits can't trigger it.
2. It writes a hibernation marker (session id, tmux pane, reset time — which
   the cache already knows) and spawns a detached watchdog. The statusline
   shows `⏾`.
3. At reset + a settle delay, the watchdog resumes the session: it types a
   continue prompt into the original tmux pane if it's still alive (your
   visible session just picks back up), else falls back to
   `claude --resume <session-id>`.

Messages you send while capped are covered too: each one fires a Stop event
that arms hibernation if nothing had yet, and the resume prompt tells the
session to answer anything you left before continuing its task.

`hibernate_watchdog.py --status` shows what's armed, `--disarm` cancels,
`--dry-run` previews what a firing would do.

## Config reference

`~/.claude/quota-router/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `weekly_protect_pct` | 75 | 7-day usage above this → protect mode |
| `fivehour_soft_pct` | 85 | 5-hour usage + launch reserve above this → constrained |
| `claude_cache_ttl_seconds` | 90 | Claude numbers older than this (and absent from the latest tick) count as stale |
| `codex_old_snapshot_seconds` | 1800 | Codex snapshots older than this get the `~` marker |
| `statusline_style` | minimal | `minimal`, `braille`, `circles`, or `plain` |
| `statusline_color` | true | ANSI colors on the readout |
| `statusline_show_pct` | 75 | minimal style: hide quota below this, show both providers at it |
| `statusline_git_symbols` | auto | `auto` (SF Symbols on macOS, unicode elsewhere), `sf`, or `ascii` |
| `statusline_accent` | #D97757 | minimal style: fallback hex for the model name. Per repo it auto-follows a custom Claude Code theme (`"theme": "custom:<name>"` in the project's `.claude` settings → `~/.claude/themes/<name>.json` `overrides.claude`); an explicit `statusline_accent` key in project settings or a `STATUSLINE_ACCENT` env var overrides the theme |
| `hibernate_enabled` | false | Opt-in auto-resume after a usage-limit stop |
| `hibernate_settle_seconds` | 90 | Extra wait past the reset before resuming |
| `hibernate_max_wait_hours` | 12 | Never hibernate longer than this |
| `fable_available_on_plan` | false | Set true only if a promo/frontier model actually shows in your model selector; gates the top Claude tier |
| `test_override` | null | Short-lived fake quota values for dry-running the routing policy |

## Limitations, honestly

- **Claude numbers come from the status-line payload.** Claude Code pushes
  `rate_limits` there on subscription plans; if yours doesn't, the Claude
  side stays `--` and only the Codex half is useful.
- **Codex numbers are as fresh as your last Codex run.** The probe reads
  rollout logs; it refuses to guess past a window reset, so after a few idle
  days Codex reads "unknown" even though it's probably sitting at 0%. One
  trivial Codex run refreshes it.
- **The per-tier reserves are eyeballed**, not billing data. They're routing
  buffers; expect to nudge them for your usage patterns.
- **The routing hook is advisory.** It puts the numbers in front of the model
  at decision time, and the skill tells it what to do with them — but nothing
  hard-blocks a delegation. In practice the model follows numbers it can see;
  this is a well-informed habit, not an enforcement layer.
- **Hibernate detection is regex-over-undocumented-events.** The exact event
  Claude Code emits at a cap isn't documented, so the hook logs everything it
  sees to `events.log` — if your first real cap slips past the detection, the
  log is exactly what's needed to fix it.
- **Both payload formats are undocumented** and could change in any release.
  The scripts fail toward "unknown", never toward a crash in your status
  line, but a format change will blank the readout until updated.

## Where this came from

It was built in one Claude Code session and adversarially reviewed across the
fence: Claude (Opus) wrote the plan, GPT reviewed it and found six real bugs
before anything shipped — among them windows collapsed with `max()`
(destroying the weekly/5-hour distinction) and rolled-over snapshots being
read as 0% used. The skill's cross-provider review rule automates the loop
that built it.

The paranoia about background Codex runs is also earned. An unbounded
"go plan this" run once hung for an hour on a broker bug, which is why the
skill insists on the companion broker path, explicit time/evidence budgets,
and a watchdog instead of waiting forever.
