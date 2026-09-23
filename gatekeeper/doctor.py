"""Health checks for a gate: catch the silent failures that fail-open would hide.

A gate that cannot reach Jev lets everything through, which is the right
behaviour for a session and the wrong one to leave unnoticed. `doctor` looks
for every known way the gate can quietly stop working:

  rulebook     schema, lint errors and warnings
  api-key      TYPESAFE_API_KEY can be found (never printed)
  model        the pinned model still answers, and what jev-latest resolves to
  latency      live latency against the rulebook's timeout
  hooks        where the hooks are installed, and that their command still exists
  skills       broken skill symlinks (a moved repo), and the gatekeeper skill itself
  mode         shadow / advise / enforce / off
  activity     judged prompts, fail-open rate, hook errors over recent days
  passthrough  harness messages that slipped past `passthrough` and got judged
  roster       installed handlers that no rule, family, or neutral entry covers
  bench        age of the last bench, rulebook changes since, unreviewed labels

Each check returns ok, info, warn, or fail with a one-line fix. Any fail makes
`gatekeeper doctor` exit 1, so it can run from cron or CI.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import jev, roster as roster_mod, rulebook, store
from .roster import claude_dir, matches_any

STATUSES = ("ok", "info", "warn", "fail")
FAIL_OPEN_WARN = 0.05   # more than 1 in 20 judged prompts fell back to fail-open
FAIL_OPEN_FAIL = 0.50   # most prompts are not being judged at all
MIN_EVENTS = 5          # below this, rates are noise
BENCH_MAX_AGE_DAYS = 30


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _ok(name, detail):
    return Check(name, "ok", detail)


# ---------- individual checks ----------

def check_rulebook(book: dict) -> Check:
    from .cli import lint  # local import: cli imports doctor

    errors, warnings = lint(book)
    if errors:
        return Check("rulebook", "fail", f"{len(errors)} lint errors, first: {errors[0]}", f"gatekeeper lint {book['gate']}")
    if warnings:
        return Check("rulebook", "warn", f"{len(warnings)} lint warnings, first: {warnings[0]}", f"gatekeeper lint {book['gate']}")
    return _ok("rulebook", f"v{book['version']} valid, lint clean")


def check_key() -> Check:
    try:
        jev.load_key()
    except jev.JevError:
        return Check("api-key", "fail", "TYPESAFE_API_KEY not in the environment or the env file",
                     "export TYPESAFE_API_KEY, set GATEKEEPER_ENV_FILE, or put it in ~/.config/gatekeeper/env")
    return _ok("api-key", "found (value not shown)")


PROBE_Q = {"probe": {"type": "noul", "instructions": "Is `text` a greeting?"}}


def check_model(book: dict, client=None) -> list[Check]:
    """Two tiny live calls: the pinned model, and jev-latest to spot a newer version."""
    client = client or jev.call
    pinned = book["model"]
    out = []
    start = time.monotonic()
    try:
        resp = client({"text": "hello"}, PROBE_Q, pinned, book.get("timeout_s", 4))
        latency = time.monotonic() - start
    except Exception as err:
        return [Check("model", "fail", f"pinned model {pinned} did not answer: {err}",
                      "check TypeSafe status and GET /v1/models; if the pin was retired, re-bench on a current version and update `model:`")]
    served = resp.get("model", "?")
    if served != pinned and not pinned.endswith("latest"):
        out.append(Check("model", "warn", f"asked for {pinned}, served {served}", "re-bench, then pin the served version"))
    else:
        out.append(_ok("model", f"{pinned} answers"))
    budget = book.get("timeout_s", 4)
    status = "warn" if latency > 0.75 * budget else "ok"
    out.append(Check("latency", status, f"{latency * 1000:.0f} ms against a {budget}s timeout",
                     "raise timeout_s or check the network" if status == "warn" else ""))
    try:
        latest = client({"text": "hello"}, PROBE_Q, "jev-latest", budget).get("model", "?")
        if latest not in (served, pinned):
            out.append(Check("model-latest", "info", f"jev-latest now serves {latest} (pinned {pinned})",
                             f"optional: bench with model {latest} before moving the pin"))
        else:
            out.append(_ok("model-latest", f"jev-latest serves {latest}, same as the pin"))
    except Exception as err:
        out.append(Check("model-latest", "info", f"could not resolve jev-latest: {err}"))
    return out


def _settings_files(extra_projects) -> list[Path]:
    files = [claude_dir() / "settings.json", rulebook.REPO / ".claude" / "settings.json"]
    for proj in extra_projects or []:
        files.append(Path(proj).expanduser() / ".claude" / "settings.json")
    seen, out = set(), []
    for f in files:
        key = str(f.resolve()) if f.exists() else str(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _python_of(argv: list[str]) -> str:
    """The interpreter of a hook command, past an `env NAME=value` prefix."""
    rest = argv[1:] if argv and argv[0] == "env" else argv
    while rest and "=" in rest[0] and not rest[0].startswith(("/", ".")):
        rest = rest[1:]
    return rest[0] if rest else "python3"


def _interpreter_problem(python: str) -> str | None:
    """Can the hook's interpreter import gatekeeper's dependencies? A hook that
    cannot is skipped on every prompt, which looks exactly like a quiet gate."""
    try:
        done = subprocess.run([python, "-c", "import yaml, jsonschema"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"hook interpreter {python} cannot run: {err}"
    if done.returncode:
        return f"hook interpreter {python} lacks PyYAML or jsonschema"
    return None


def check_hooks(book: dict, extra_projects=None) -> Check:
    found, broken = [], []
    this = rulebook.identity(book.get("_path") or book["gate"])
    interpreters: dict = {}
    for path in _settings_files(extra_projects):
        try:
            hooks = json.loads(path.read_text()).get("hooks", {})
        except (OSError, json.JSONDecodeError):
            continue
        events = set()
        for event, groups in hooks.items():
            for group in groups:
                for hook in group.get("hooks", []):
                    try:
                        argv = shlex.split(hook.get("command", ""))
                    except ValueError:
                        continue
                    if not any(a.endswith("bin/gatekeeper") for a in argv) or "--gate" not in argv:
                        continue
                    i = argv.index("--gate") + 1
                    if i >= len(argv) or rulebook.identity(argv[i]) != this:
                        continue
                    events.add(event)
                    bins = [a for a in argv if a.endswith("bin/gatekeeper")]
                    if not Path(bins[0]).exists():
                        broken.append(f"{path}: {bins[0]} missing")
                    else:
                        python = _python_of(argv)
                        if python not in interpreters:
                            interpreters[python] = _interpreter_problem(python)
                        if interpreters[python]:
                            broken.append(f"{path}: {interpreters[python]}")
        if events:
            missing = {"UserPromptSubmit", "PreToolUse"} - events
            found.append(f"{path.parent.parent if path.parent.name == '.claude' else path.parent}"
                         + (f" (missing {', '.join(sorted(missing))})" if missing else ""))
    if broken:
        return Check("hooks", "fail", "; ".join(dict.fromkeys(broken)),
                     "re-run scripts/install_hooks.py --apply for that location, with the Python that has the dependencies")
    if not found:
        return Check("hooks", "warn", "not installed anywhere checked, so the gate never runs",
                     "python3 scripts/install_hooks.py --project <repo> --apply (or --global)")
    partial = [f for f in found if "missing" in f]
    if partial:
        return Check("hooks", "warn", "installed but incomplete: " + "; ".join(partial), "re-run scripts/install_hooks.py --apply")
    return _ok("hooks", "installed in " + "; ".join(found))


def check_skills() -> Check:
    """A skill symlink left behind by a moved repo silently drops that skill,
    and the lint warnings it causes look like a rulebook problem."""
    skills = claude_dir() / "skills"
    broken = sorted(p.name for p in skills.iterdir() if p.is_symlink() and not (p / "SKILL.md").exists()) if skills.is_dir() else []
    ours = skills / "gatekeeper"
    fix = "re-link each to its new location, e.g. ln -sfn <new path> ~/.claude/skills/<name>"
    if "gatekeeper" in broken:
        return Check("skills", "fail", "the gatekeeper skill link points at a missing folder",
                     "python3 scripts/install_hooks.py --skill --apply")
    if not (ours / "SKILL.md").exists():
        return Check("skills", "warn" if broken else "info",
                     "the gatekeeper skill is not installed"
                     + (f"; {len(broken)} other skill links are broken: {', '.join(broken)}" if broken else ""),
                     "python3 scripts/install_hooks.py --skill --apply" + (f"; {fix}" if broken else ""))
    if broken:
        return Check("skills", "warn", f"{len(broken)} other skill links point at missing folders: {', '.join(broken)}", fix)
    if ours.resolve() != rulebook.REPO.resolve():
        return Check("skills", "warn", f"~/.claude/skills/gatekeeper is {ours.resolve()}, not this repo",
                     "python3 scripts/install_hooks.py --skill --apply")
    return _ok("skills", "no broken skill links; gatekeeper skill installed")


def check_mode(book: dict) -> Check:
    detail = {"shadow": "shadow: logs what it would do, changes nothing",
              "advise": "advise: adds the verdict as context, never blocks or denies",
              "enforce": "enforce: injects routes and denies calls",
              "off": "off: hooks do nothing"}[book["mode"]]
    return Check("mode", "info", detail)


def _recent(rows: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["ts"]) >= cutoff:
                out.append(r)
        except (KeyError, ValueError):
            continue
    return out


def check_activity(book: dict, rows: list[dict], days: int, hooks_installed: bool) -> list[Check]:
    rows = [r for r in _recent(rows, days) if r.get("gate") in (book["gate"], None) or r.get("action") == "error"]
    prompts = [r for r in rows if r.get("hook") == "UserPromptSubmit" and r.get("verdict")]
    judged = [r for r in prompts if not str(r["verdict"].get("decided_by", "")).startswith("rule:")]
    failed = [r for r in judged if str(r["verdict"].get("decided_by", "")).startswith("fail:")]
    errors = [r for r in rows if r.get("action") == "error"]
    out = []
    if not prompts:
        if hooks_installed and book["mode"] != "off":
            out.append(Check("activity", "warn", f"hooks installed but nothing logged in {days} days",
                             "check that sessions run where the hooks are installed; try `gatekeeper hook` by hand"))
        else:
            out.append(Check("activity", "info", f"no prompts logged in {days} days"))
    elif len(judged) < MIN_EVENTS:
        out.append(Check("activity", "info", f"{len(prompts)} prompts, {len(judged)} judged by Jev in {days} days (too few to rate)"))
    else:
        rate = len(failed) / len(judged)
        last = failed[-1]["verdict"].get("error", "") if failed else ""
        detail = f"{len(judged)} judged in {days} days, {rate:.0%} fell back to fail-open" + (f"; last: {last[:120]}" if last else "")
        if rate > FAIL_OPEN_FAIL:
            out.append(Check("activity", "fail", detail, "the gate is mostly not judging: run doctor online and check the model and key"))
        elif rate > FAIL_OPEN_WARN:
            out.append(Check("activity", "warn", detail, "look at the last error; check TypeSafe status and timeout_s"))
        else:
            out.append(_ok("activity", detail))
    if errors:
        out.append(Check("hook-errors", "warn", f"{len(errors)} hook errors in {days} days, last: {errors[-1].get('error', '')[:120]}",
                         "gatekeeper log --tail 50"))
    return out


def check_passthrough(book: dict, rows: list[dict], days: int) -> Check:
    """Judged prompts that look like harness messages mean `passthrough` has drifted."""
    judged = [r for r in _recent(rows, days) if r.get("hook") == "UserPromptSubmit" and r.get("verdict")]
    suspicious = [r for r in judged if str(r.get("prompt_excerpt", "")).lstrip().startswith(("<", "[SYSTEM"))]
    if suspicious:
        sample = suspicious[-1]["prompt_excerpt"][:60]
        return Check("passthrough", "warn", f"{len(suspicious)} judged prompts look like harness messages, e.g. {sample!r}",
                     "add the new tag to `passthrough` in the rulebook")
    return _ok("passthrough", "no harness-looking prompts were judged")


def check_roster(book: dict) -> Check:
    offered = roster_mod.build(book.get("roster", {}))
    groups = {e.get("group") for e in offered.values() if e.get("group")}
    if not offered:
        return Check("roster", "fail", "the roster is empty, so the Choice has nothing to pick",
                     f"gatekeeper roster {book['gate']} --all")
    if set((book.get("roster") or {}).get("sources") or []) == {"index"}:
        ungrouped = sorted(n for n, e in offered.items() if not e.get("group"))
        detail = f"{len(offered)} handlers from the index in {len(groups)} groups"
        if ungrouped and any(q.get("rollup") for q in (book.get("judgments") or {}).values()):
            return Check("roster", "info", f"{detail}; {len(ungrouped)} have no group: {', '.join(ungrouped[:10])}",
                         "optional: give them a group so rollup can place them")
        return _ok("roster", detail)
    covered = [p for ps in (book.get("families") or {}).values() for p in ps] + list(book.get("neutral") or [])
    named = {c for rule in book.get("hard_rules") or [] for c in rule.get("candidates", [])}
    loose = sorted(n for n in offered if n not in named and not matches_any(n, covered))
    if loose:
        return Check("roster", "info", f"{len(offered)} handlers; {len(loose)} have no rule or family: {', '.join(loose)}",
                     "optional: add a candidate rule so keyword prompts reach them")
    return _ok("roster", f"{len(offered)} handlers, all covered by a rule, family, or neutral entry")


def check_bench(book: dict) -> Check:
    reports = sorted((store.home() / "bench").glob(f"{book['gate']}-*.json"))
    if not reports:
        return Check("bench", "warn", "never benched", f"gatekeeper bench {book['gate']}")
    last = json.loads(reports[-1].read_text())
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ran_at"])).days
    problems, fixes = [], []
    if last.get("version") != book["version"]:
        problems.append(f"rulebook is v{book['version']}, last bench was v{last.get('version')}")
        fixes.append(f"gatekeeper bench {book['gate']}")
    if last.get("model") != book["model"]:
        problems.append(f"model is {book['model']}, last bench used {last.get('model')}")
        fixes.append(f"gatekeeper bench {book['gate']}")
    if age > BENCH_MAX_AGE_DAYS:
        problems.append(f"last bench is {age} days old")
        fixes.append(f"gatekeeper bench {book['gate']}")
    if "claude-draft" in (last.get("label_sources") or []):
        problems.append("labels are unreviewed Claude drafts")
        fixes.append("review bench labels, then set label_source to reviewed")
    summary = f"last bench {age}d ago, v{last.get('version')}, accuracy {last.get('accuracy')}"
    if problems:
        return Check("bench", "warn", summary + "; " + "; ".join(problems), "; ".join(dict.fromkeys(fixes)))
    return _ok("bench", summary)


# ---------- runner ----------

def run(gate: str, offline: bool = False, days: int = 7, projects=None, client=None) -> list[Check]:
    try:
        book = rulebook.load(gate, validate=False)
    except rulebook.RulebookError as err:
        return [Check("rulebook", "fail", str(err), "check the gate name or path")]
    checks = [check_rulebook(book), check_key()]
    if offline:
        checks.append(Check("model", "info", "skipped (--offline)"))
    elif checks[-1].status != "fail":
        checks += check_model(book, client)
    hooks = check_hooks(book, projects)
    checks += [hooks, check_skills(), check_mode(book)]
    rows = store.read_log(days=days + 1)
    checks += check_activity(book, rows, days, hooks_installed=hooks.status in ("ok", "warn") and "not installed" not in hooks.detail)
    checks += [check_passthrough(book, rows, days), check_roster(book), check_bench(book)]
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0


def render(checks: list[Check]) -> str:
    mark = {"ok": "ok  ", "info": "info", "warn": "WARN", "fail": "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"[{mark[c.status]}] {c.name:<13} {c.detail}")
        if c.fix and c.status in ("warn", "fail", "info"):
            lines.append(f"{'':21}fix: {c.fix}")
    counts = {s: sum(c.status == s for c in checks) for s in STATUSES}
    verdict = "UNHEALTHY" if counts["fail"] else "needs attention" if counts["warn"] else "healthy"
    lines.append(f"\n{verdict}: {counts['ok']} ok, {counts['info']} info, {counts['warn']} warn, {counts['fail']} fail")
    return "\n".join(lines)


def to_json(checks: list[Check]) -> str:
    return json.dumps({"healthy": exit_code(checks) == 0, "checks": [asdict(c) for c in checks]}, indent=2)
