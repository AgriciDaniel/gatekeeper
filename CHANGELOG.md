# Changelog

## 0.2.1 (2026-09-23)

Audit fixes (security, docs, coverage). Tests in `tests/test_audit_security.py` and `tests/test_audit_coverage.py`.

- Security: the TypeSafe key is stripped and validated (a stray CR could make an HTTP header error echo it into the log), every request error is reported without its message, and errors are masked before they are logged.
- Security: masking now catches the loaded TypeSafe key exactly, plus JWTs, credentials in URLs, `curl -u`, Stripe `sk_live_`, GitLab, AWS `ASIA`, Slack app tokens and webhooks, Google OAuth, Hugging Face, npm, SendGrid, unterminated private keys and JSON-style `"apiKey": "..."` secrets.
- Fail-open: any failure on the hook path, including an argument error from a stale `settings.json`, exits 0. Exit code 2 would have made Claude Code block the prompt. The installer rejects gates that start with `-`.
- Index rosters: handler names must be plain identifiers, and `path` must stay relative inside the index's tree (no absolute paths, `~`, `..` or shell characters), because both are echoed into the assistant's context.
- The key file moves from `<repo>/.env` to `~/.config/gatekeeper/env`, since the skill link exposes the repo folder under `~/.claude/skills`.
- The log, state and bench reports are written with owner-only permissions (0700 folders, 0600 files).
- An explicit `forbid` now outranks `neutral`, as the starter rulebook says.
- `bench` on a gate with no bench file says so instead of crashing (the starter has none).
- README art in `docs/assets/`: an animated banner, router and rollup diagram (CSS and SMIL only, so GitHub renders them), with embedded OFL font subsets and their licenses.
- `SECURITY.md`, `CONTRIBUTING.md`, issue templates (bug report, gate idea) and a pull request checklist.
- Docs: the README shows real, unabridged output, the privacy section lists everything that is sent, and SKILL.md, the CLI help and the starter's route message match the code.

## 0.2.0 (2026-09-23)

Portable release: nothing in the repo assumes one machine or one person's agents.

- New `index` roster source: route to handlers listed in a JSON file (name, description, group, path, optional `where` filter) instead of Claude Code agents.
- New `rollup: group` on a Choice: probabilities are summed by roster group, and decide rules can use `group_band` / `group_band_in`. A vague request still reaches the right role when no single handler is confident.
- New `advise` mode: adds the verdict as context but never blocks a prompt or denies a call.
- Private overrides: a gate name resolves to `gates/local/<gate>.yaml` (git-ignored) first, then `gates/`, then `examples/`. Bench and index paths resolve relative to the rulebook.
- `gates/agent-selection.yaml` is now a generic starter that works on any install. Personal rules move to `gates/local/`.
- New `examples/marketing-team/`: 24 skills in 11 roles, an advise-mode gate with rollup, and a 20-row bench.
- Key lookup: `TYPESAFE_API_KEY`, then `GATEKEEPER_ENV_FILE`, then `<repo>/.env`. No machine-specific default path.
- Verdict state is kept per gate, so two gates in one session never overwrite each other.
- `doctor` checks for skill symlinks left behind by a moved repo and whether the gatekeeper skill is installed, reports index rosters by group, and fails on an empty roster.
- `install_hooks.py`: `--skill` links `~/.claude/skills/gatekeeper`; `--gate` accepts a rulebook path (quoted safely in the hook command).
- The repo's own `.claude/settings.json` is no longer tracked; hooks are installed per machine.
- MIT license, CI on Python 3.11 to 3.13, packaging metadata. Gatekeeper runs from a checkout (editable install); outside one, the CLI says so instead of failing later.

Fixed after a fresh-context review (regression tests mostly in `tests/test_review_v02.py`):

- The hook command names the Python that ran the installer (a venv keeps working) and quotes the script path. A hook whose interpreter lacks a dependency exits 0 instead of erroring on every prompt, and `doctor` fails on it.
- `doctor` finds hooks installed by rulebook path, not only by gate name.
- Installing a gate by name and by path is recognized as one gate, so hooks are never doubled.
- A malformed index file (a scalar, bad JSON, bad UTF-8) yields an empty roster instead of a crash; a string `description` field works; `entries` becomes kind `entry`.
- `gatekeeper mode` never edits a shipped rulebook; the mode is saved in `gates/local/modes.json`.
- Lint rejects a roster group named like the escape option, and `group_band` on a judgment without `rollup: group`.
- Stale verdict files and v0.1 state files are pruned.
- A broken link to another skill is a `doctor` warning; only a broken gatekeeper link fails.
- Tests use neutral handler names and never read private rulebooks or key files.

## 0.1.0 (2026-09-22)

First version: YAML rulebooks, hard rules, one batched Jev request per event, confidence bands, a decision table, Claude Code UserPromptSubmit and PreToolUse hooks, shadow and enforce modes, fail-open, bench, log, and `doctor`. Passed an adversarial review; every confirmed finding has a regression test in `tests/test_review_regressions.py`.
