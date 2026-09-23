---
name: gatekeeper
description: >
  Rule-driven decision gates for anything that needs a verdict before it
  proceeds: agent selection, routing, triage, approvals. Each gate is a YAML
  rulebook: deterministic hard rules decide what code can decide, Jev (TypeSafe
  System One) answers the semantic questions as typed Choice, Noul, or Score
  judgments, and confidence bands turn answers into allow, route, suggest,
  confirm, escalate, or block. Rosters come from installed Claude Code agents
  and skills or from a JSON index, with rollup by group. Runs as Claude Code
  hooks in shadow, advise, or enforce mode (ships in shadow). Use for
  "/gatekeeper", "gatekeeper", "which agent should handle this", "why was that
  blocked", "tune the gate", "bench the gate", "write a new gate", "add a rule",
  "is the gate healthy", or "gatekeeper doctor".
argument-hint: "doctor | judge | explain | roster | lint | bench | log | mode | new-gate"
---

# Gatekeeper

Repo: this skill's folder (installed as a symlink at `~/.claude/skills/gatekeeper`).
CLI: `python3 ~/.claude/skills/gatekeeper/bin/gatekeeper <command>`, or `bin/gatekeeper` from the repo.

A gate name resolves to `gates/local/<gate>.yaml` (private, git-ignored) first,
then `gates/<gate>.yaml`, then `examples/<gate>/<gate>.yaml`. A path to a YAML
file works anywhere a gate name does, except `log --gate`, which takes the gate's `gate:` name.

```
event -> hard_rules (code) -> judgments (one Jev request) -> decide (first match) -> verdict + log
```

## Commands

| Ask | Run |
|---|---|
| What would the gate decide? | `bin/gatekeeper judge <gate> "<text>" [--project P] [--json]` |
| Why did it decide that? | `bin/gatekeeper explain <gate> "<text>"` |
| Which handlers can it pick? | `bin/gatekeeper roster <gate> [--all]` |
| Is the rulebook sound? | `bin/gatekeeper lint <gate>` |
| How good are the thresholds? | `bin/gatekeeper bench <gate> [--file F]` (live; about $0.00006 per row on a 24-handler roster, $0.0004 at 143) |
| What happened recently? | `bin/gatekeeper log [--tail N]` |
| Turn enforcement on or off | `bin/gatekeeper mode <gate> shadow|advise|enforce|off` |
| Is it still working? | `bin/gatekeeper doctor [gate] [--offline] [--json] [--days N] [--project P]` (exit 1 on any FAIL) |

`judge`, `explain`, and `bench` call Jev and need `TYPESAFE_API_KEY`. The
client reads it from the environment, then the file named by
`GATEKEEPER_ENV_FILE`, then `~/.config/gatekeeper/env`. Never print it.

## Editing a rulebook (`gates/<gate>.yaml`)

1. Read the rulebook and `gates/_schema.json` first.
2. Put anything a regex or comparison can decide in `hard_rules`. Put only the
   semantic part in `judgments`.
3. One narrow question per judgment. A Choice needs an escape option when
   nothing may fit.
4. Bands are per judgment and follow the cost of a wrong call. They stay
   untuned guesses until `bench` over reviewed labels says otherwise.
5. Bump `version` whenever a rule, band, or option changes, so the log shows
   which rulebook made each decision.
6. Run `bin/gatekeeper lint <gate>` and `python3 -m pytest -q` before reporting done.

## New gate

Copy the shape of `gates/agent-selection.yaml`: `state.fields` names the event
fields Jev sees, `judgments` holds the questions, and `decide` maps answers to
verdicts. A new domain (for example email triage) needs a new YAML file plus
whatever adapter turns its input into an event dict. The engine does not change.

## Rosters from an index

A gate can route to Markdown procedures instead of Claude Code agents: set
`roster.sources: [index]` and point `roster.index.file` at a JSON list (fields
for name, description, group, and path are configurable). Add `rollup: group`
to the Choice and the engine also sums probabilities by group, so a decide rule
can use `group_band: act` when the exact handler is unclear but its group is
not. See `examples/marketing-team/`. Such gates run in `advise` mode.

## Enforcement (agent-selection)

The hooks are installed with `scripts/install_hooks.py` (`--global` or
`--project`, dry run by default). `shadow` only logs what it would inject or
deny. `advise` injects the verdict as context but never blocks or denies. `enforce` injects the
route as context and denies Agent or Skill calls that break an act-band route
or a `forbid` rule. `GATEKEEPER=off` disables every hook at once. The hooks
fail open: a Jev outage never blocks work. A handler named in the prompt is
never denied. Harness messages (`passthrough`) are never judged. See README
"Safety rules".

## Keep it working

The gate fails open, so a broken gate looks exactly like a quiet one. Run
`bin/gatekeeper doctor` monthly, after installing or removing agents or skills,
after a Claude Code update, and always before `mode ... enforce`. It checks the
key, that the pinned model still answers, what `jev-latest` now serves, hook
installs, the fail-open rate in the log, harness messages that slipped past
`passthrough`, handlers no rule covers, and whether the bench is stale.

- FAIL: fix before anything else. The gate is not judging.
- WARN: fix soon. Each line carries its own `fix:`.
- INFO: a choice for the user (for example a newer Jev version to bench).
- Never move `model:` or a band without a fresh bench on the new setting.

## Rules

- Gatekeeper decides who handles work. It never sends, publishes, or spends.
- Typed output guarantees the shape, not the truth. Tune on labelled data.
- Only a hand-reviewed bench can move a threshold. Draft labels
  (`label_source: claude-draft`) are a starting point.
- Re-check the Jev model, limits, and price against
  `GET https://api.typesafe.ai/v1/models` and docs.typesafe.ai before relying on them.
