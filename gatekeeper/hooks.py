"""Claude Code hook adapters: stdin JSON in, stdout JSON out.

UserPromptSubmit  evaluates the gate on the prompt, stores the verdict for the
                  session, adds it as context (advise and enforce modes), and
                  blocks the prompt on a `block` verdict (enforce mode only).
PreToolUse        on Agent/Task/Skill, denies a call that breaks this turn's
                  verdict: a forbidden handler, or a specialist outside the
                  routed one's family when the route is in the act band.

Mode comes from the rulebook: `shadow` logs what it would do and outputs
nothing; `advise` adds the verdict as context but never blocks or denies;
`enforce` acts; `off` does nothing. GATEKEEPER=off in the
environment is a kill switch. A hook must never break a session, so any
internal error is logged and the hook exits quietly.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import engine, roster as roster_mod, rulebook, store
from .roster import base, matches_any
from .redact import redact
from .rules import match

AGENT_TOOLS = {"Agent": "subagent_type", "Task": "subagent_type", "Skill": "skill"}


def context_text(v: dict) -> str:
    if not v.get("message"):
        return ""
    return f"Gatekeeper ({v['gate']} v{v['version']}): {v['message']}"


def families_of(name: str, families: dict) -> set[str]:
    return {fam for fam, patterns in (families or {}).items() if matches_any(name, patterns)}


def named_in(prompt: str, names) -> list[str]:
    """Handlers the user names in the prompt ("ask the code-agent", "use code agent")."""
    found = []
    for name in names:
        short = base(name)
        variants = {short, short.replace("-", " "), name}
        if any(re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", prompt, re.I) for v in variants):
            found.append(name)
    return found


def violation(book: dict, v: dict, name: str) -> str | None:
    """Why calling `name` breaks verdict `v`, or None if it is allowed.

    Never denies when the gate failed open (a Jev outage must not block work)
    or when the user named the handler in the prompt (the user outranks the gate)."""
    if str(v.get("decided_by", "")).startswith("fail:"):
        return None
    if any(base(n) == base(name) for n in v.get("named") or []):
        return None
    hits = [f for f in v.get("forbid") or [] if matches_any(name, [f])]
    if hits:  # an explicit forbid outranks `neutral`
        rules = sorted({(v.get("forbid_by") or {}).get(f, "?") for f in hits})
        return f"`{name}` is forbidden for this request by rule {', '.join(rules)}."
    if matches_any(name, book.get("neutral", [])):
        return None
    if v.get("kind") == "block":
        return f"this request was blocked by {v.get('decided_by')}: {v.get('message')}"
    target = v.get("target")
    if v.get("kind") != "route" or not target:
        return None
    if base(name) == base(target) or families_of(name, book.get("families")) & families_of(target, book.get("families")):
        return None
    conf = next((a.get("confidence") for a in (v.get("answers") or {}).values() if a.get("choice") == target), None)
    shown = f" (confidence {conf:.2f})" if isinstance(conf, (int, float)) else ""
    return (
        f"the {v['gate']} rulebook routed this request to `{target}`{shown}, "
        f"and `{name}` is outside that family. Use `{target}`, or ask the user to name a different handler."
    )


def user_prompt(book: dict, payload: dict) -> dict | None:
    prompt = payload.get("prompt") or ""
    cwd = payload.get("cwd") or os.getcwd()
    session = payload.get("session_id")
    if any(match(p, {"prompt": prompt}) for p in book.get("passthrough") or []):
        store.log({"hook": "UserPromptSubmit", "gate": book["gate"], "mode": book["mode"], "action": "passthrough",
                   "session_id": session, **store.fingerprint(prompt)})
        return None  # harness message: not the user's words, and the turn's verdict stays in force
    store.clear_verdict(session, book["gate"])  # if this hook dies, no stale verdict from the last turn survives
    event = {"prompt": prompt, "cwd": cwd, "project": Path(cwd).name, "session_id": session}
    roster = roster_mod.build(book.get("roster", {}))
    verdict = engine.evaluate(book, event, roster=roster).to_dict()
    verdict["named"] = named_in(prompt, roster_mod.build({**book.get("roster", {}), "exclude": []}))
    store.save_verdict(session, verdict)

    enforce = book["mode"] == "enforce"
    speaks = book["mode"] in ("enforce", "advise")  # advise: context only, never blocks
    text = context_text(verdict)
    out = None
    if verdict["kind"] == "block" and enforce:
        action = "block"
        out = {"decision": "block", "reason": text or "Blocked by Gatekeeper."}
    elif verdict["kind"] == "block" and not speaks:
        action = "would_block"
    elif text:
        action = "inject" if speaks else "would_inject"
        if speaks:
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    else:
        action = "none"
    store.log({"hook": "UserPromptSubmit", "gate": book["gate"], "mode": book["mode"], "action": action,
               "session_id": payload.get("session_id"), "project": event["project"], **store.fingerprint(prompt),
               "verdict": verdict})
    return out


def pre_tool(book: dict, payload: dict) -> dict | None:
    tool = payload.get("tool_name")
    name = (payload.get("tool_input") or {}).get(AGENT_TOOLS.get(tool, ""), "")
    if not name:
        return None
    verdict = store.load_verdict(payload.get("session_id"), book["gate"])
    if verdict is None or verdict.get("gate") != book["gate"]:
        return None
    reason = violation(book, verdict, name)
    enforce = book["mode"] == "enforce"
    action = ("deny" if enforce else "would_deny") if reason else "allow"
    store.log({"hook": "PreToolUse", "gate": book["gate"], "mode": book["mode"], "action": action,
               "session_id": payload.get("session_id"), "tool": tool, "name": name, "reason": reason,
               "verdict_kind": verdict.get("kind"), "verdict_target": verdict.get("target")})
    if reason and enforce:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"Gatekeeper: {reason}"}}
    return None


HANDLERS = {"user-prompt": user_prompt, "pre-tool": pre_tool}


def run(kind: str, gate: str, stdin_text: str) -> str:
    if os.environ.get("GATEKEEPER", "").lower() == "off":
        return ""
    try:
        book = rulebook.load(gate, validate=False)
        if book.get("mode") == "off":
            return ""
        out = HANDLERS[kind](book, json.loads(stdin_text or "{}"))
        return json.dumps(out) if out else ""
    except Exception as err:  # never break the session
        try:
            store.log({"hook": kind, "gate": gate, "action": "error", "error": redact(f"{type(err).__name__}: {err}")[:300]})
        except Exception:
            pass
        return ""
