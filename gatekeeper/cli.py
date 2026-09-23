"""gatekeeper: rule-driven decision gates, judged by Jev.

  gatekeeper judge   <gate> "<text>" [--project P] [--json]  verdict for one event
  gatekeeper explain <gate> "<text>" [--project P]            verdict plus every rule, option, and answer
  gatekeeper roster  <gate> [--all]                   handlers the gate can route to
  gatekeeper lint    <gate>                           schema, names, bands, decision table
  gatekeeper bench   <gate> [--file F] [--limit N]    live run over labelled rows (needs the key)
  gatekeeper log     [--tail N] [--gate G]            recent decisions
  gatekeeper mode    <gate> shadow|advise|enforce|off switch a gate's mode
  gatekeeper doctor  [gate] [--offline] [--json] [--days N] [--project P]
                                                      health checks; exits 1 on any failure
  gatekeeper hook    user-prompt|pre-tool --gate G    Claude Code hook entrypoint (stdin JSON)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bench, doctor, engine, hooks, roster as roster_mod, rulebook, store


def _event(args) -> dict:
    project = args.project or ""
    return {"prompt": args.text, "project": project, "cwd": project}


def cmd_judge(args) -> int:
    book = rulebook.load(args.gate)
    v = engine.evaluate(book, _event(args))
    if args.json:
        print(json.dumps(v.to_dict(), indent=2))
        return 0
    head = f"{v.kind}" + (f" -> {v.target}" if v.target else "")
    print(f"verdict:    {head}")
    for qid, ans in v.answers.items():
        detail = f"{ans.get('choice', '')} conf {ans['confidence']}" if "confidence" in ans else f"p(yes) {ans['noul']}"
        print(f"{qid + ':':<12}{detail.strip()} [{ans['band']}]")
        if ans.get("group_choice"):
            print(f"{'  group:':<12}{ans['group_choice']} conf {ans['group_confidence']} [{ans['group_band']}]")
        if ans.get("path"):
            print(f"{'  path:':<12}{ans['path']}")
    print(f"decided by: {v.decided_by}")
    if v.message:
        print(f"message:    {v.message}")
    if v.error:
        print(f"error:      {v.error}")
    print(f"cost:       ${v.cost_usd:.6f}, {v.latency_ms} ms")
    return 0


def cmd_explain(args) -> int:
    book = rulebook.load(args.gate)
    v = engine.evaluate(book, _event(args))
    print(f"gate {book['gate']} v{book['version']}  model {book['model']}  mode {book['mode']}  fail {book['fail']}")
    print(f"rules fired:  {', '.join(v.rules_fired) or 'none'}")
    print(f"candidates:   {', '.join(v.candidates) or 'none (full roster)'}")
    print(f"forbidden:    {', '.join(v.forbid) or 'none'}")
    if not v.decided_by.startswith("rule:"):
        qs = engine.build_questions(book, v.candidates, v.forbid)
        for qid, q in qs.items():
            n = len(q.get("criteria") or {}) if q["type"] == "choice" else ""
            print(f"question {qid}: {q['type']}" + (f" over {n} options" if n else ""))
    for qid, ans in v.answers.items():
        print(f"answer {qid}: {json.dumps(ans)}")
    print(f"decided by:   {v.decided_by}")
    print(f"verdict:      {v.kind}" + (f" -> {v.target}" if v.target else ""))
    if v.message:
        print(f"inject:       {hooks.context_text(v.to_dict())}")
    if v.error:
        print(f"error:        {v.error}")
    print(f"cost ${v.cost_usd:.6f}, {v.input_tokens} input tokens, {v.latency_ms} ms")
    return 0


def cmd_roster(args) -> int:
    book = rulebook.load(args.gate)
    cfg = dict(book.get("roster", {}))
    if args.all:
        cfg["exclude"] = []
    r = roster_mod.build(cfg)
    for name, e in r.items():
        print(f"{e['kind']:<6} {name:<36} {e['description'][:90]}")
    print(f"\n{len(r)} handlers" + ("" if args.all else " offered (use --all to include excluded leaves)"))
    return 0


def lint(book: dict) -> tuple[list[str], list[str]]:
    errors = rulebook.schema_errors({k: v for k, v in book.items() if not k.startswith("_")})
    warnings: list[str] = []
    if errors:
        return errors, warnings
    full = roster_mod.build({**book.get("roster", {}), "exclude": []})
    offered = roster_mod.build(book.get("roster", {}))
    known = set(full) | set(book.get("neutral", []))

    ids = [r["id"] for r in book.get("hard_rules", [])]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"hard rule id {dup!r} is used twice")
    for rule in book.get("hard_rules", []):
        for name in rule.get("candidates", []) + rule.get("forbid", []):
            if not any(ch in name for ch in "*?[") and name not in full:
                warnings.append(f"rule {rule['id']}: {name!r} is not an installed agent or skill")
    for fam, patterns in (book.get("families") or {}).items():
        for p in patterns:
            if not roster_mod.matches_any_name(p, known):
                warnings.append(f"family {fam}: {p!r} matches nothing installed")

    judgments = book.get("judgments") or {}
    for qid, spec in judgments.items():
        b = spec.get("bands") or {}
        need = {"yes", "no"} if spec["type"] == "noul" else {"act", "confirm"}
        if b and set(b) != need:
            errors.append(f"judgment {qid}: bands must have exactly {sorted(need)}, got {sorted(map(str, b))}")
            continue
        if spec["type"] == "noul" and b and not b.get("yes", 1) > b.get("no", 0):
            errors.append(f"judgment {qid}: noul band yes must be above no")
        if spec["type"] != "noul" and b and not b.get("act", 1) >= b.get("confirm", 0):
            errors.append(f"judgment {qid}: act must be at or above confirm")
        if spec["type"] == "choice" and spec.get("options", "roster") == "roster":
            size = len(offered) + len(spec.get("escape") or {})
            if size > engine.MAX_CHOICE_OPTIONS:
                errors.append(f"judgment {qid}: roster of {size} exceeds {engine.MAX_CHOICE_OPTIONS} options")
            elif size > 120 and spec.get("rollup") != "group":
                warnings.append(f"judgment {qid}: roster of {size} options; confidence spreads thin on large rosters (consider rollup: group)")
            if spec.get("rollup") == "group" and not any(e.get("group") for e in offered.values()):
                errors.append(f"judgment {qid}: rollup: group, but no roster entry has a group")
            clash = {e.get("group") for e in offered.values()} & set(spec.get("escape") or {})
            if spec.get("rollup") == "group" and clash:
                errors.append(f"judgment {qid}: roster group {sorted(clash)[0]!r} has the escape option's name; rename one")
    index = (book.get("roster") or {}).get("index")
    if index and not roster_mod._index_entries(book["roster"]):
        errors.append(f"roster index {index.get('file')!r} is missing, unreadable, or has no matching entries")

    def refs(cond):
        if not isinstance(cond, dict):
            return
        if "judgment" in cond:
            yield ("judgment", cond["judgment"])
        if "rule" in cond:
            yield ("rule", cond["rule"])
        for key in ("all", "any"):
            for sub in cond.get(key, []):
                yield from refs(sub)
        if "not" in cond:
            yield from refs(cond["not"])

    valid_bands = {"noul": {"yes", "no", "uncertain"}, "choice": {"act", "confirm", "escalate"},
                   "score": {"act", "confirm", "escalate"}}

    def band_refs(cond):
        if not isinstance(cond, dict):
            return
        if "judgment" in cond:
            for b in [cond.get("band"), cond.get("group_band")] + list(cond.get("band_in") or []) + list(cond.get("group_band_in") or []):
                if b is not None:
                    yield cond["judgment"], b
        for key in ("all", "any"):
            for sub in cond.get(key, []):
                yield from band_refs(sub)
        if "not" in cond:
            yield from band_refs(cond["not"])

    def group_refs(cond):
        if not isinstance(cond, dict):
            return
        if "judgment" in cond and ("group_band" in cond or "group_band_in" in cond):
            yield cond["judgment"]
        for key in ("all", "any"):
            for sub in cond.get(key, []):
                yield from group_refs(sub)
        if "not" in cond:
            yield from group_refs(cond["not"])

    for i, entry in enumerate(book["decide"]):
        for qid in group_refs(entry.get("when")):
            if qid in judgments and judgments[qid].get("rollup") != "group":
                errors.append(f"decide {entry.get('id', i)}: group_band on {qid!r}, which has no rollup: group, never matches")
        for kind, ref in refs(entry.get("when")):
            if (kind == "judgment" and ref not in judgments) or (kind == "rule" and ref not in ids):
                errors.append(f"decide {entry.get('id', i)}: unknown {kind} {ref!r}")
        for qid, band in band_refs(entry.get("when")):
            if qid in judgments and band not in valid_bands[judgments[qid]["type"]]:
                errors.append(f"decide {entry.get('id', i)}: {band!r} is not a band of {judgments[qid]['type']} judgment {qid!r}")
    if book["decide"][-1].get("when"):
        warnings.append("decide has no unconditional last entry; unmatched events fall through to allow")
    return errors, warnings


def cmd_lint(args) -> int:
    book = rulebook.load(args.gate, validate=False)
    errors, warnings = lint(book)
    for e in errors:
        print(f"error:   {e}")
    for w in warnings:
        print(f"warning: {w}")
    print(f"{book['gate']}: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


def cmd_bench(args) -> int:
    book = rulebook.load(args.gate)
    rows = bench.load_rows(book, args.file)[: args.limit or None]
    report = bench.run(book, rows)
    path = bench.save(report)
    print(f"{report['gate']} v{report['version']} on {report['model']}: {report['n']} rows, accuracy {report['accuracy']}")
    print(f"labels: {', '.join(report['label_sources'])}")
    print("by band:")
    for band, s in report["bands"].items():
        print(f"  {band:<24} n={s['n']:<4} accuracy={s['accuracy']}")
    print("by confidence:")
    for b in report["confidence_buckets"]:
        if b["n"]:
            print(f"  {b['range']}  n={b['n']:<4} accuracy={b['accuracy']}")
    print(f"current bands: {report['current_bands']}")
    print(f"suggested act threshold at {report['target_precision']} precision: {report['suggested_act_threshold']}")
    print(f"verdicts: {report['verdicts']}  errors: {report['errors']}")
    print(f"latency p50 {report['latency_ms']['p50']} ms, p95 {report['latency_ms']['p95']} ms; cost ${report['cost_usd']}")
    if report["misses"]:
        print("misses:")
        for m in report["misses"]:
            print(f"  {m['id']}: expected {m['expect']}, got {m['got']} ({m['confidence']}, {m['band']})")
    print(f"report: {path}")
    return 0


def cmd_log(args) -> int:
    rows = [r for r in store.read_log() if not args.gate or r.get("gate") == args.gate][-args.tail:]
    for r in rows:
        if r.get("hook") == "PreToolUse":
            print(f"{r['ts']}  pre-tool  {r['action']:<12} {r.get('tool')}({r.get('name')})  {r.get('reason') or ''}")
        elif r.get("action") == "error":
            print(f"{r['ts']}  {r.get('hook')}  error  {r.get('error')}")
        else:
            v = r.get("verdict", {})
            tgt = f" -> {v.get('target')}" if v.get("target") else ""
            print(f"{r['ts']}  prompt    {r['action']:<12} {v.get('kind')}{tgt}  [{v.get('decided_by')}]  {r.get('prompt_excerpt', '')[:60]}")
    return 0


def cmd_mode(args) -> int:
    path = rulebook.set_mode(args.gate, args.mode)
    print(f"{args.gate}: mode {args.mode} (saved in {path})")
    return 0


def cmd_doctor(args) -> int:
    checks = doctor.run(args.gate, offline=args.offline, days=args.days, projects=args.project)
    print(doctor.to_json(checks) if args.json else doctor.render(checks))
    return doctor.exit_code(checks)


def cmd_hook(args) -> int:
    out = hooks.run(args.kind, args.gate, sys.stdin.read())
    if out:
        print(out)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gatekeeper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("judge", "explain"):
        s = sub.add_parser(name)
        s.add_argument("gate")
        s.add_argument("text")
        s.add_argument("--project", default="")
        s.add_argument("--json", action="store_true")
    s = sub.add_parser("roster")
    s.add_argument("gate")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("lint")
    s.add_argument("gate")
    s = sub.add_parser("bench")
    s.add_argument("gate")
    s.add_argument("--file")
    s.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("log")
    s.add_argument("--tail", type=int, default=30)
    s.add_argument("--gate")
    s = sub.add_parser("mode")
    s.add_argument("gate")
    s.add_argument("mode", choices=rulebook.MODES)
    s = sub.add_parser("doctor")
    s.add_argument("gate", nargs="?", default="agent-selection")
    s.add_argument("--offline", action="store_true", help="skip the live Jev calls")
    s.add_argument("--json", action="store_true")
    s.add_argument("--days", type=int, default=7, help="how far back to read the log")
    s.add_argument("--project", action="append", help="also look for hooks in this repo (repeatable)")
    s = sub.add_parser("hook")
    s.add_argument("kind", choices=sorted(hooks.HANDLERS))
    s.add_argument("--gate", required=True)
    args = p.parse_args(argv)
    if not rulebook.GATES.is_dir() and args.cmd != "hook":
        print("gatekeeper: no gates/ folder next to the package. Gatekeeper runs from a git checkout:\n"
              "  git clone https://github.com/AgriciDaniel/gatekeeper && cd gatekeeper && pip install -e .",
              file=sys.stderr)
        return 2
    try:
        return {"judge": cmd_judge, "explain": cmd_explain, "roster": cmd_roster, "lint": cmd_lint, "bench": cmd_bench,
                "log": cmd_log, "mode": cmd_mode, "doctor": cmd_doctor, "hook": cmd_hook}[args.cmd](args)
    except rulebook.RulebookError as err:
        print(f"gatekeeper: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
