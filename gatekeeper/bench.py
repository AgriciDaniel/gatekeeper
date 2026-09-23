"""Run a gate over hand-labelled events and report how far its bands can be trusted.

Each bench row is JSON: {"id", "prompt", "project", "expect", "label_source"}.
`expect` is the handler the router should pick (the escape name, such as
main_thread, when no specialist should). A pick counts as correct when it is
the expected handler or in the same family.

The report answers the threshold question from data instead of the docs'
illustrative numbers: accuracy per band, accuracy per confidence bucket, and
the lowest act threshold that keeps precision at the rulebook's target.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import engine, roster as roster_mod, store
from .hooks import families_of
from .rulebook import RulebookError, relative_to_book

BUCKETS = [0.0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0001]


def load_rows(book: dict, file: str | None = None) -> list[dict]:
    if not file and not (book.get("bench") or {}).get("file"):
        raise RulebookError(f"{book['gate']} has no bench file: pass --file rows.jsonl, or add a `bench:` "
                            f"section to your copy in gates/local/")
    path = Path(file) if file else relative_to_book(book, book["bench"]["file"])
    if not path.exists():
        raise RulebookError(f"bench file {path} does not exist")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def same_handler(book: dict, got: str | None, expect: str) -> bool:
    if got is None:
        return False
    if roster_mod.base(got) == roster_mod.base(expect):
        return True
    return bool(families_of(got, book.get("families")) & families_of(expect, book.get("families")))


def run(book: dict, rows: list[dict], client=None) -> dict:
    cfg = book.get("bench", {})
    qid = cfg.get("judgment", "target")
    escape = next(iter(book["judgments"][qid].get("escape") or {"main_thread": ""}))
    roster = roster_mod.build(book.get("roster", {}))
    results = []
    for row in rows:
        event = {"prompt": row["prompt"], "project": row.get("project", ""), "cwd": row.get("project", "")}
        v = engine.evaluate(book, event, client=client, roster=roster)
        ans = v.answers.get(qid, {})
        got = ans.get("choice") or (escape if v.decided_by.startswith("rule:") and v.kind == "allow" else None)
        results.append({
            "id": row.get("id"), "expect": row["expect"], "got": got, "correct": same_handler(book, got, row["expect"]),
            "escape": got == escape,
            "confidence": ans.get("confidence"), "band": ans.get("band") or v.decided_by, "verdict": v.kind,
            "rules": v.rules_fired, "latency_ms": v.latency_ms, "cost_usd": v.cost_usd, "error": v.error,
            "label_source": row.get("label_source", ""),
        })
    return summarize(book, results)


def summarize(book: dict, results: list[dict]) -> dict:
    cfg = book.get("bench", {})
    target_precision = cfg.get("target_precision", 0.95)
    min_rows = cfg.get("min_rows", 10)
    judged = [r for r in results if r["confidence"] is not None]
    # Only specialist picks are gated by the act band; escape picks are allowed at any confidence.
    gated = [r for r in judged if not r.get("escape")]

    def acc(rs):
        return round(sum(r["correct"] for r in rs) / len(rs), 3) if rs else None

    by_band = {}
    for r in results:
        by_band.setdefault(r["band"], []).append(r)
    buckets = []
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        rs = [r for r in judged if lo <= r["confidence"] < hi]
        buckets.append({"range": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(rs), "accuracy": acc(rs)})

    suggested = None
    for t in sorted({r["confidence"] for r in gated}):
        above = [r for r in gated if r["confidence"] >= t]
        if len(above) >= min_rows and acc(above) >= target_precision:
            suggested = {"act": t, "n_at_or_above": len(above), "precision": acc(above),
                         "coverage_of_specialist_picks": round(len(above) / len(gated), 3)}
            break

    kinds = {}
    for r in results:
        kinds[r["verdict"]] = kinds.get(r["verdict"], 0) + 1
    latencies = [r["latency_ms"] for r in results if r["confidence"] is not None]
    return {
        "gate": book["gate"], "version": book["version"], "model": book["model"],
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(results), "accuracy": acc(results),
        "bands": {b: {"n": len(rs), "accuracy": acc(rs)} for b, rs in sorted(by_band.items())},
        "confidence_buckets": buckets,
        "current_bands": book["judgments"][cfg.get("judgment", "target")].get("bands"),
        "suggested_act_threshold": suggested,
        "target_precision": target_precision,
        "verdicts": kinds,
        "errors": sum(1 for r in results if r["error"]),
        "latency_ms": {"p50": int(statistics.median(latencies)) if latencies else None,
                       "p95": sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None},
        "cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        "label_sources": sorted({r["label_source"] for r in results}),
        "misses": [r for r in results if not r["correct"]],
        "rows": results,
    }


def save(report: dict) -> Path:
    path = store.private_dir(store.home() / "bench") / f"{report['gate']}-{report['ran_at'].replace(':', '')}.json"
    store.private_write(path, json.dumps(report, indent=2) + "\n")
    return path
