# Gatekeeper Agent Instructions

Read `SKILL.md`, then `README.md`, then the rulebook you are touching in `gates/`.

## Rules

- Code owns the workflow. Jev answers only the semantic questions a rule cannot.
- Thresholds live in rulebooks, never in Python. Move one only with a bench
  report over hand-reviewed labels, and bump the rulebook `version`.
- Gatekeeper decides who handles work and whether it may proceed. It never
  takes outside actions (send, publish, deploy, spend) itself.
- Hooks must fail open and must never break a session.
- Never print or store `TYPESAFE_API_KEY`. The log stores a prompt hash and a
  short excerpt, never the full prompt.
- Installing hooks into `~/.claude/settings.json` (global) needs the user's
  explicit approval. Project installs start in `shadow` mode.

## Verification

```bash
python3 -m pytest -q
bin/gatekeeper lint agent-selection
bin/gatekeeper lint marketing-team
bin/gatekeeper bench marketing-team    # live; needs the key (your own gate: add a bench file)
```
