---
name: subagent-router
description: Use before every delegation or subagent launch — Agent/Task calls, background agents, codex-rescue, /codex commands, or any decision to run work on Claude vs Codex or pick a model/effort/fan-out. Reads live Claude and Codex quota and returns the provider, subagent role, model, effort, and concurrency to use. Triggers on "delegate", "subagent", "spin up", "spawn", "hand off", "codex", "route this", "which model", and before parallel fan-out.
---

# Subagent Router

Route delegated work by **live quota first, model strength second**. Claude subagents
all draw the same Claude account pool; only Codex draws a separate (ChatGPT) pool — so
when Claude is constrained, the lever is to offload to Codex, not to spawn more Claude
subagents (concurrency multiplies the draw).

A `PreToolUse` hook already injects the current snapshot at every `Agent` call, so the
numbers are in front of you. This skill is how you *act* on them.

## Posture: orchestrate, don't execute

The orchestrator's own turns are the most expensive spend there is — frontier model ×
full context × every turn (field data 2026-07-16: two interactive frontier sessions
burned a full 5-hour window inline while Codex sat at 21%). Keep the main thread for
decomposition, judgment, review, and synthesis; delegate execution aggressively:

- Multi-file implementation, broad reads/searches, test runs, mechanical edits: always
  delegable. Inline only what's genuinely a one-liner.
- Cheap delegation is the point, not a compromise — a Haiku/spark agent that needs a
  correction round still costs less than the orchestrator doing the work inline.
- The question is rarely *whether* to delegate; it's *which provider and tier* — which
  is what the gates below answer.

**Delegation is not quota-conditional.** The gates and bands below choose the *target* —
provider, model, effort, fan-out. None of them decide *whether* to delegate; low usage is
not a license to work inline. A Fable orchestrator at 5% of every window still delegates
execution — its own turns are the most expensive spend on the account at any usage level.

**When the orchestrator itself is Fable:** subagents launched through the Agent tool
**inherit the parent model unless `model` is set explicitly** — an override-less launch
from a Fable session runs the subagent on Fable and spends the Fable weekly sub-limit,
which defeats the entire point of delegating. From a Fable main thread, every Agent call
pins an explicit model from the tier table (sonnet / haiku / opus); a Fable-tier launch
happens only for a frontier-class task that passes the full Fable gate. This is a hard
rule, not a preference — treat an unpinned Agent call as a bug.

## Read the state

1. Run `python3 ~/.claude/quota-router/codex_quota_probe.py` → Codex windows.
2. Read `~/.claude/quota-cache.json` → Claude `five_hour` / `seven_day` (`used_percentage`,
   `resets_at`, `observed_at`, `present_in_latest_payload`).
3. Read `~/.claude/quota-router/config.json` → thresholds, `fable_available_on_plan`,
   `test_override`. If `test_override.expires_epoch` is in the future, use its values and
   label the decision **TEST**; ignore it if expired.
4. **Fable weekly estimate:** Fable has its own weekly sub-limit ≈ `fable_weekly_fraction`
   (default 0.5) of the account's 7-day window, and it is **not exposed in the status
   payload** — so estimate it: `fable_est = seven_day.used ÷ fable_weekly_fraction`,
   capped at 100. This is a worst-case upper bound (it assumes all weekly usage was
   Fable); that is the safe direction for a protect gate. No fresh seven_day → the
   estimate is unknown, and unknown is not headroom.
5. **Freshness / unknown:** a Claude window is unknown if missing, or stale
   (`present_in_latest_payload:false` AND age > `claude_cache_ttl_seconds`). A Codex window
   is unknown if `routing_available:false` (rolled-over/implausible) or the snapshot age >
   `codex_old_snapshot_seconds`. **Never treat unknown or rolled-over as 0% / headroom.**

## Classify the task

| Class | Recognition |
|---|---|
| Frontier | hardest one-shot, clear acceptance criteria, unusually high value |
| Hard reasoning | ambiguous root cause, architecture, gnarly debugging |
| Standard | clear implementation, ordinary bug, contained refactor |
| Mechanical | search/replace, formatting, narrow extraction (usually keep inline) |
| Adversarial review | find mistakes / challenge — route to whichever model did NOT write it |

Tier table (quota permitting):

| Class | Claude | Codex |
|---|---|---|
| Frontier | Fable 5 max (after Fable gate) else Opus 4.8 max | gpt-5.6-sol xhigh — **bounded implementation only** |
| Hard reasoning | Opus 4.8 high; xhigh only with ample headroom | gpt-5.6-sol high |
| Standard | Sonnet 5 medium | gpt-5.6-sol medium |
| Mechanical | Haiku 5 low (inline only if a true one-liner) | gpt-5.3-codex-spark low |
| Adversarial review | Claude only if Codex wrote it | Codex only if Claude wrote it |

**Final pre-merge review is two jobs, not one:** the adversarial bug-hunt goes to the
provider that did NOT write the branch (per-task reviews by same-provider subagents do
not count as the cross-check); the orchestrator then judges the findings and makes the
merge call. Never substitute the orchestrator's own re-read for the cross-provider pass.

(The model names are whatever the two plans expose today — edit this table when
yours differ. The gates below don't care what the tiers are called.)

## The gate (raw usage + reserve is the HARD capacity check)

Per-tier **5h reserve** (rough share of the 5-hour window a launch consumes; add to the
five_hour projection before gating — a routing buffer, not a billing figure):

| Fable max | Opus max | Opus high/xhigh | Sonnet med | Haiku low | Codex xhigh | Codex high | Codex med | spark |
|---|---|---|---|---|---|---|---|---|
| +12 | +8 | +6 | +3 | +1 | +10 | +6 | +3 | +1 |

Gate **each window separately** — do NOT collapse them with `max()`; the weekly and
5-hour thresholds are different and need the window's identity. Identify Codex windows by
`role` (`short`≈5h, `long`≈weekly from `window_minutes`), not primary/secondary.

For a candidate Claude tier with reserve `R`:
- **Weekly gate:** `seven_day.used > weekly_protect_pct` (default 75) → **weekly-binding**.
  (A single launch barely moves a 7-day window, so weekly uses raw used, no reserve — the
  point is you're already near the wall.)
- **5h gate:** `five_hour.used + R > fivehour_soft_pct` (default 85) → **5h-constrained**.

**Reset proximity only decides wait-vs-switch — it never discounts the gate.** "Resets
soon" = `minutes_to_reset ≤ max(20, 5% of window)`.

- **Fable weekly gate:** `fable_est > fable_weekly_protect_pct` (default 80) →
  **Fable-weekly-binding**. This can fire long before the account weekly gate does (at the
  0.5 fraction it trips when raw 7d passes ~40%), and it binds *both directions*: no
  Fable-tier subagent launches, and the orchestrator's own Fable turns are now the
  scarcest resource — batch decisions, keep turns short, delegate everything executable,
  and tell the user switching the main session to Opus (`/model`) would relieve it.

**Conserve band:** `five_hour.used ≥ fivehour_conserve_pct` (default 50) → not yet
constrained, but delegation *targets* shift: standard and mechanical work goes to Codex
(or Haiku) by default, and Claude tiers above Haiku are reserved for tasks that need
Claude specifically. This is what keeps the window from filling before the hard gates
ever fire — by the time 85% trips, the cheap offloading should already have happened.

## Decide

Apply in order; stop at the first that fires:

1. **Both providers constrained** (any Claude gate fires AND a Codex window is binding):
   pick the lower-pressure provider at a reduced tier, serialize (fan-out 1), or wait for
   the earliest useful reset. Do not thrash between providers. *(This must be checked
   first — rules 2–4 each assume the other provider still has headroom.)*
2. **Weekly-binding (Claude 7d > weekly_protect_pct):** protect hard. Disable Fable. Route
   heavy/parallel work to Codex (if Codex is unknown, fan-out 1 and conservative tiers).
   Anything that must stay on Claude drops one tier. Claude fan-out = 1.
3. **Codex-binding** (Codex binding window `used + codex_reserve` over its threshold —
   weekly >75 or short >85): keep work on Claude; if you must use Codex, drop its effort
   first. Normal Claude fan-out.
4. **Claude 5h-constrained** for the candidate tier: (a) if 5h resets soon and the task can
   wait → **wait**; (b) elif a cheaper Claude tier's projection ≤ `fivehour_soft_pct` →
   **drop one tier**; (c) else → **offload to Codex**. Claude fan-out ≤ 2 (usually 1).
5. **One provider unknown:** prefer the provider with confirmed headroom; fan-out 1.
6. **Both unknown:** no frontier delegation; conservative tier + the 429 backstop, or wait.
7. **Both available:** use the task-class prior. Model strength is only a tiebreak —
   Claude for interactive ambiguity / sustained orchestration context; Codex for bounded
   implementation with immutable inputs and executable checks. Cross-provider review
   overrides. Claude fan-out up to 3 for independent work. **In the conserve band,
   the prior flips for standard/mechanical work: Codex (or Haiku) unless the task
   needs Claude.**

**Burst awareness:** raw-usage + reserve is always the hard gate, but if a window is
climbing fast within its period (e.g. two recent readings jumped several points), treat it
as one tier more constrained than the snapshot alone suggests, especially before fan-out.

## Fable gate (all must pass, else Claude ceiling = Opus 4.8 max)

1. `fable_available_on_plan` is true.
2. Task is frontier-class.
3. Weekly is not binding.
4. **Fable weekly estimate is not binding** (`fable_est ≤ fable_weekly_protect_pct`).
5. 5h is available.
6. `five_hour.used + 12` still leaves 5h available.
7. No Fable entitlement/limit error this session.

A Fable entitlement error → mark Fable unavailable for the session, retry Opus immediately,
do not re-probe entitlement.

## Launching Codex (companion-broker path — NOT the codex-rescue Agent)

Use the companion broker so you get a real job id + watchdog. The codex-rescue Agent's
`--background` backgrounds the *Claude* subagent and does not give you a broker job to poll.

```
node "$(ls ~/.claude/plugins/*/openai-codex/codex/*/scripts/codex-companion.mjs 2>/dev/null | head -1)" \
  task --background --model <model> --effort <effort> --prompt-file <file>
# add --write ONLY if edits are intended (omit for read-only)
```
- Always: `--background`, explicit `--model`, explicit `--effort`, read-only unless writes intended.
- Name **immutable inputs** (files, commits, copied evidence). Never point Codex at live
  session files or "the newest rollout"; never send "go figure it out" without bounded inputs.
- State an evidence budget + time budget + expected output + stop condition in the prompt.
- Reserve **xhigh for bounded implementation with acceptance checks**; cap planning /
  exploration / open-ended debugging at **high**.
- Poll with `... status <job-id>` / `... result <job-id>`; cancel with `... cancel <job-id>`.
  Trust only the status command's own state/exit fields — never grep output or logs for
  completion-sounding words; word-matching is what produces false "it's done" detections.
- **Watchdog:** if the job exceeds its time budget + 2 min, or `status` stops advancing /
  the broker vanishes, stop waiting, re-read quota, and reroute once (do not wait forever —
  this is the failure mode that hung a run for an hour).
- **Keep the plumbing out of chat.** Job ids, poll loops, watchdog arming/re-arming, broker
  paths, and recoveries from your own polling mistakes are internal — the user should see
  what's running and when results are expected ("Codex is reviewing the branch; findings in
  ~20 min"), then the findings. Mention the machinery only when it changes what the user
  gets: the job failed, was cancelled, or got rerouted.

## If THIS session hits its usage cap

Do **not** self-schedule wakeups to wait out a quota window — ScheduleWakeup
clamps at 1 hour, so you wake still-capped, stall, and never see the real
reset. Stop cleanly instead: the hibernate watchdog arms off the quota cache
and resumes this session just after the true reset (including responding to
any messages the user left while you were capped).

## 429 / limit backstop

On a provider limit error: mark that provider **binding for this decision** (re-read the
cache, but note re-reading only reuses the last pushed Claude payload — it is not a fresh
account query, so trust the live error over the snapshot). Retry **once** on the other
provider at the same-or-cheaper tier. If both fail, stop and report the earliest known
reset. Never loop or increase fan-out after a limit error.

## Output of a routing decision

State: task class → provider → subagent role (Explore=read-only discovery, Plan=planning,
project/general-purpose=impl/debug, companion-broker=Codex) → model → effort → fan-out →
one-line why (which gate fired). If a `test_override` is active, prefix with **TEST**.
