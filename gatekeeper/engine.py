"""Evaluate one event against one rulebook.

    event -> hard rules (code) -> Jev judgments (one batched request)
          -> decision table (first match wins) -> Verdict

The engine is pure apart from the Jev call, which is injected so tests can
replay recorded answers. It never acts on a verdict; hooks and callers do.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

from . import jev, roster as roster_mod
from .bands import confidence_band, noul_band
from .redact import redact
from .rules import match

MAX_CHOICE_OPTIONS = 255  # docs.typesafe.ai/primitives/choice, retrieved 2026-09-20
DEFAULT_TIMEOUT_S = 4.0


class GateError(RuntimeError):
    pass


@dataclass
class Verdict:
    gate: str
    version: int
    kind: str  # allow | route | suggest | confirm | escalate | block
    target: str | None = None
    message: str = ""
    decided_by: str = ""
    rules_fired: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    forbid_by: dict = field(default_factory=dict)  # forbidden name -> rule id that forbade it
    answers: dict = field(default_factory=dict)
    model: str | None = None
    input_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _Empty:
    """A missing template value: renders as nothing, whatever the format spec."""

    def __getattr__(self, name):
        return self

    def __format__(self, spec):
        return ""


class _Attrs:
    """Template context for one judgment: unknown attributes render empty."""

    def __init__(self, data: dict):
        self.__dict__.update(data)

    def __getattr__(self, name):
        return _Empty()

    def __format__(self, spec):
        return format(self.__dict__.get("choice", ""), spec)


class _Context(dict):
    def __missing__(self, key):
        return _Empty()


def _render(template: str | None, answers: dict) -> str:
    """Fill {judgment.field} from the answers. A missing field renders empty
    instead of leaving the whole message as raw template text."""
    if not template:
        return ""
    try:
        return template.format_map(_Context({k: _Attrs(v) for k, v in answers.items()}))
    except (ValueError, IndexError, TypeError):
        return template


def _choice_options(spec: dict, book: dict, candidates: list, forbid: list, roster: dict | None) -> dict:
    if spec.get("options", "roster") == "roster":
        if roster is None:
            roster = roster_mod.build(book.get("roster", {}))
        pool = {name: entry["description"] or None for name, entry in roster.items()}
    else:
        pool = dict(spec["options"])
    if candidates and spec.get("narrow_to_candidates", True):
        pool = {name: pool.get(name) for name in candidates}
    options = {name: desc for name, desc in pool.items() if not roster_mod.matches_any(name, forbid)}
    options.update(spec.get("escape") or {})
    if len(options) > MAX_CHOICE_OPTIONS:
        raise GateError(f"{len(options)} options exceeds the {MAX_CHOICE_OPTIONS}-option Choice limit; narrow the roster")
    if len(options) < 2:
        raise GateError(f"Choice needs at least two options, got {sorted(options)}")
    return options


def build_questions(book: dict, candidates: list, forbid: list, roster: dict | None = None) -> dict:
    questions = {}
    for qid, spec in (book.get("judgments") or {}).items():
        q = {"type": spec["type"], "instructions": spec["instructions"]}
        if spec["type"] == "choice":
            q["criteria"] = _choice_options(spec, book, candidates, forbid, roster)
        elif spec.get("criteria") is not None:
            q["criteria"] = spec["criteria"]
        questions[qid] = q
    return questions


def build_state(book: dict, event: dict) -> dict:
    limit = book["state"].get("max_chars", 8000)
    state = {}
    for name in book["state"]["fields"]:
        value = event.get(name)
        if value is None:
            continue
        state[name] = redact(value)[:limit] if isinstance(value, str) else value
    return state


def _call_with_deadline(client, state, questions, model, timeout_s):
    """Run the Jev call on a daemon thread and stop waiting at the deadline.

    urlopen's timeout does not bound DNS resolution, so a resolver hang could
    otherwise outlive the hook's budget. A daemon thread never delays exit."""
    box: dict = {}

    def work():
        try:
            box["out"] = client(state, questions, model, timeout_s)
        except BaseException as err:  # re-raised on the caller's thread
            box["err"] = err

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout_s + 0.5)
    if worker.is_alive():
        raise GateError(f"no answer within {timeout_s}s")
    if "err" in box:
        raise box["err"]
    return box["out"]


def _rollup(probabilities: dict, roster: dict, escape: dict, bands: dict) -> dict:
    """Sum a Choice's probabilities by roster group: when no single handler is
    confident, the group they share can still be (hierarchical classification).
    The escape option is its own group."""
    totals: dict = {}
    for name, p in probabilities.items():
        group = name if name in escape else (roster.get(name) or {}).get("group")
        if group:
            totals[group] = totals.get(group, 0.0) + float(p)
    if not totals:
        return {}
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    top, conf = ranked[0]
    return {
        "group_choice": top,
        "group_confidence": round(min(conf, 1.0), 4),
        "group_band": confidence_band(min(conf, 1.0), bands) if bands else "",
        "group_alternatives": ", ".join(f"{k} {v:.2f}" for k, v in ranked[:3]),
    }


def normalize(book: dict, raw: dict, roster: dict | None = None) -> dict:
    out = {}
    for qid, spec in (book.get("judgments") or {}).items():
        ans = raw.get(qid)
        if ans is None:
            raise GateError(f"answer for {qid!r} missing from Jev response")
        bands = spec.get("bands") or {}
        if spec["type"] == "noul":
            p = float(ans["noul"])
            out[qid] = {"type": "noul", "noul": round(p, 4), "band": noul_band(p, bands) if bands else ""}
            continue
        conf = float(ans["confidence"])
        entry = {"type": spec["type"], "confidence": round(conf, 4), "band": confidence_band(conf, bands) if bands else ""}
        ranked = sorted((ans.get("probabilities") or {}).items(), key=lambda kv: -kv[1])
        entry["alternatives"] = ", ".join(f"{k} {v:.2f}" for k, v in ranked[:3])
        if spec["type"] == "choice":
            entry["choice"] = ans["choice"]
            entry["is_escape"] = ans["choice"] in (spec.get("escape") or {})
            entry["probabilities"] = dict(ranked[:5])
            picked = (roster or {}).get(ans["choice"]) or {}
            for key in ("group", "path"):
                if picked.get(key):
                    entry[key] = picked[key]
            if spec.get("rollup") == "group":
                entry.update(_rollup(dict(ranked), roster or {}, spec.get("escape") or {}, bands))
        else:
            entry["score"] = round(float(ans["score"]), 4)
        out[qid] = entry
    return out


def condition(cond: dict | None, fired: set, answers: dict) -> bool:
    if not cond:
        return True
    if "all" in cond:
        return all(condition(c, fired, answers) for c in cond["all"])
    if "any" in cond:
        return any(condition(c, fired, answers) for c in cond["any"])
    if "not" in cond:
        return not condition(cond["not"], fired, answers)
    if "rule" in cond:
        return cond["rule"] in fired
    ans = answers.get(cond.get("judgment"))
    if ans is None:
        return False
    checks = []
    if "band" in cond:
        checks.append(ans.get("band") == cond["band"])
    if "band_in" in cond:
        checks.append(ans.get("band") in cond["band_in"])
    if "choice" in cond:
        checks.append(ans.get("choice") == cond["choice"])
    if "choice_in" in cond:
        checks.append(ans.get("choice") in cond["choice_in"])
    if "group_band" in cond:
        checks.append(ans.get("group_band") == cond["group_band"])
    if "group_band_in" in cond:
        checks.append(ans.get("group_band") in cond["group_band_in"])
    if "choice_is_escape" in cond:
        checks.append(bool(ans.get("is_escape")) == cond["choice_is_escape"])
    return all(checks) if checks else False


def evaluate(book: dict, event: dict, client=None, roster: dict | None = None) -> Verdict:
    client = client or jev.call
    started = time.monotonic()
    v = Verdict(gate=book["gate"], version=book["version"], kind="allow")

    candidates: list[str] = []
    forbid: list[str] = []
    for rule in book.get("hard_rules") or []:
        if not match(rule["when"], event):
            continue
        v.rules_fired.append(rule["id"])
        candidates += [c for c in rule.get("candidates", []) if c not in candidates]
        for f in rule.get("forbid", []):
            if f not in forbid:
                forbid.append(f)
                v.forbid_by[f] = rule["id"]
        if "verdict" in rule:
            v.kind, v.target, v.message = rule["verdict"], rule.get("target"), rule.get("message", "")
            v.decided_by = f"rule:{rule['id']}"
            break
    v.candidates, v.forbid = candidates, forbid

    if not v.decided_by and book.get("judgments"):
        try:
            if roster is None and any(q.get("type") == "choice" and q.get("options", "roster") == "roster"
                                      for q in book["judgments"].values()):
                roster = roster_mod.build(book.get("roster", {}))
            questions = build_questions(book, candidates, forbid, roster)
            response = _call_with_deadline(client, build_state(book, event), questions, book["model"],
                                           book.get("timeout_s", DEFAULT_TIMEOUT_S))
            v.model = response.get("model")
            v.input_tokens = int((response.get("usage") or {}).get("input_tokens", 0))
            v.cost_usd = jev.cost_usd(v.input_tokens)
            v.answers = normalize(book, response["answers"], roster)
        except Exception as err:  # any failure takes the rulebook's fail path
            v.error = redact(f"{type(err).__name__}: {err}")[:300]
            v.latency_ms = int((time.monotonic() - started) * 1000)
            if book["fail"] == "open":
                v.kind, v.decided_by = "allow", "fail:open"
            else:
                v.kind, v.decided_by = "escalate", "fail:closed"
                v.message = f"Gatekeeper could not judge this ({v.error}). Ask before proceeding."
            return v

    if not v.decided_by:
        fired = set(v.rules_fired)
        for i, entry in enumerate(book["decide"]):
            if condition(entry.get("when"), fired, v.answers):
                v.kind = entry["verdict"]
                v.target = _render(entry.get("target"), v.answers) or None
                v.message = _render(entry.get("message"), v.answers)
                v.decided_by = f"decide:{entry.get('id', i)}"
                break
        else:
            v.kind, v.decided_by = "allow", "decide:none-matched"

    v.latency_ms = int((time.monotonic() - started) * 1000)
    return v
