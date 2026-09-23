# Contributing

Thanks for helping. Gatekeeper is small on purpose: code decides what code can decide, and Jev answers only the semantic question left.

## Before you open a pull request

```bash
pip install -e ".[test]"
python3 -m pytest -q                      # offline, no key needed
bin/gatekeeper lint agent-selection
bin/gatekeeper lint marketing-team
```

- Keep thresholds in rulebooks, never in Python. Move a band only with a bench report over hand-reviewed labels, and bump the rulebook `version`.
- Hooks must fail open and must never break a session. Add a test for any new hook path.
- Never print or store `TYPESAFE_API_KEY`.
- Do not commit private rulebooks, benches or logs. `gates/local/` and `.gatekeeper/` are git-ignored for that reason.
- Write plain, direct docs. No em dashes.

## Proposing a new gate

Open an issue with the **Gate idea** template: what event it judges, which parts code can decide, the one semantic question for Jev, and what each verdict should do.
