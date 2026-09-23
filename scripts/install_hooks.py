#!/usr/bin/env python3
"""Add or remove Gatekeeper's Claude Code hooks in a settings.json.

Dry run by default: prints the diff and changes nothing. With --apply it
backs up the file first, then merges. Existing hooks are preserved, and
running it twice adds nothing twice.

  python3 scripts/install_hooks.py --project ~/code/some-repo            # dry run
  python3 scripts/install_hooks.py --project ~/code/some-repo --apply
  python3 scripts/install_hooks.py --global --apply                          # ~/.claude/settings.json
  python3 scripts/install_hooks.py --project ~/code/some-repo --uninstall --apply
  python3 scripts/install_hooks.py --skill --apply                           # link the /gatekeeper skill
  python3 scripts/install_hooks.py --project ~/demo --gate g.yaml --home ~/demo/.gatekeeper --apply   # own log
  python3 scripts/install_hooks.py --project ~/vault --gate ~/vault/gates/routing.yaml --apply

The hooks obey the rulebook's `mode`: a fresh install in `shadow` mode only logs.
"""
from __future__ import annotations

import argparse
import difflib
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin" / "gatekeeper"
sys.path.insert(0, str(BIN.parent.parent))

from gatekeeper.rulebook import identity  # noqa: E402

# The interpreter running the installer is the one that has the dependencies
# (a venv after `pip install -e .`), so the hook command names it explicitly.
PYTHON = sys.executable or "python3"


def entries(gate: str, home: str | None = None) -> dict:
    """`home` gives these hooks their own log and state folder (GATEKEEPER_HOME)."""
    prefix = f"env GATEKEEPER_HOME={shlex.quote(home)} " if home else ""

    def cmd(kind):
        command = f"{prefix}{shlex.quote(PYTHON)} {shlex.quote(str(BIN))} hook {kind} --gate {shlex.quote(gate)}"
        return {"type": "command", "command": command, "timeout": 10}

    return {
        "UserPromptSubmit": {"hooks": [cmd("user-prompt")]},
        "PreToolUse": {"matcher": "Agent|Task|Skill", "hooks": [cmd("pre-tool")]},
    }


def hook_gate(hook: dict) -> str | None:
    """The --gate argument of a gatekeeper hook command, or None for other hooks."""
    try:
        argv = shlex.split(hook.get("command", ""))
    except ValueError:
        return None
    if not (any(a.endswith("bin/gatekeeper") for a in argv) and "hook" in argv and "--gate" in argv):
        return None
    i = argv.index("--gate") + 1
    return argv[i] if i < len(argv) else None


def is_ours(hook: dict, gate: str) -> bool:
    """This gate's gatekeeper hook, nothing else. A gate name and the path of the
    same rulebook are the same gate, so installing both ways never doubles it."""
    found = hook_gate(hook)
    return found is not None and (found == gate or identity(found) == identity(gate))


def merge(settings: dict, gate: str, uninstall: bool, home: str | None = None) -> dict:
    """Remove only our hook entries (other tools' hooks in a shared group stay),
    then add ours once. A settings file that already has them is left unchanged."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    for event, group in entries(gate, home).items():
        groups = hooks.get(event, [])
        if not uninstall and any(h == group["hooks"][0] for g in groups for h in g.get("hooks", [])):
            continue  # already installed exactly as we would install it
        kept = []
        for g in groups:
            rest = [h for h in g.get("hooks", []) if not is_ours(h, gate)]
            if rest:
                kept.append({**g, "hooks": rest})
        hooks[event] = kept if uninstall else kept + [group]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


SKILL_LINK = Path("~/.claude/skills/gatekeeper").expanduser()


def link_skill(apply: bool, uninstall: bool) -> int:
    """Symlink ~/.claude/skills/gatekeeper to this repo (or remove that link)."""
    repo = BIN.parent.parent
    current = SKILL_LINK.resolve() if SKILL_LINK.is_symlink() else None
    if SKILL_LINK.exists() and not SKILL_LINK.is_symlink():
        print(f"{SKILL_LINK} is a real folder, not a link; leaving it alone")
        return 1
    if uninstall:
        if current is None:
            print(f"{SKILL_LINK}: not linked")
            return 0
        print(f"remove {SKILL_LINK} -> {current}")
        if apply:
            SKILL_LINK.unlink()
        return 0
    if current == repo:
        print(f"{SKILL_LINK}: already linked to {repo}")
        return 0
    print(f"link {SKILL_LINK} -> {repo}" + (f" (was {current})" if current else ""))
    if apply:
        SKILL_LINK.parent.mkdir(parents=True, exist_ok=True)
        SKILL_LINK.unlink(missing_ok=True)
        SKILL_LINK.symlink_to(repo, target_is_directory=True)
    else:
        print("dry run: nothing written. Add --apply to write.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = p.add_mutually_exclusive_group()
    where.add_argument("--project", help="repo whose .claude/settings.json gets the hooks")
    where.add_argument("--global", dest="global_", action="store_true", help="~/.claude/settings.json")
    p.add_argument("--gate", default="agent-selection", help="gate name, or a path to a rulebook YAML")
    p.add_argument("--skill", action="store_true", help="also link ~/.claude/skills/gatekeeper to this repo")
    p.add_argument("--home", help="separate log and state folder for these hooks (sets GATEKEEPER_HOME)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = p.parse_args()
    if not (args.project or args.global_ or args.skill):
        p.error("choose --project, --global, or --skill")
    if args.skill:
        rc = link_skill(args.apply, args.uninstall)
        if rc or not (args.project or args.global_):
            return rc
    if args.gate.startswith("-"):
        p.error("a gate name or path must not start with '-'")
    if Path(args.gate).expanduser().suffix in (".yaml", ".yml"):
        args.gate = str(Path(args.gate).expanduser().resolve())

    path = (Path("~/.claude") if args.global_ else Path(args.project).expanduser() / ".claude").expanduser() / "settings.json"
    before = json.loads(path.read_text()) if path.exists() else {}
    home = str(Path(args.home).expanduser().resolve()) if args.home else None
    after = merge(before, args.gate, args.uninstall, home)
    a, b = json.dumps(before, indent=2).splitlines(), json.dumps(after, indent=2).splitlines()
    diff = list(difflib.unified_diff(a, b, str(path), str(path), lineterm=""))
    if not diff:
        print(f"{path}: already up to date")
        return 0
    print("\n".join(diff))
    if not args.apply:
        print("\ndry run: nothing written. Add --apply to write.")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"settings.json.bak-gatekeeper-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    path.write_text(json.dumps(after, indent=2) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
