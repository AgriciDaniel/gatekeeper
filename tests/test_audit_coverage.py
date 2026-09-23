"""Branches the 2026-09-23 audit found untested: decide conditions, shadow blocks, bench and log output."""
import json

import pytest
from conftest import FakeJev

from gatekeeper import cli, engine, hooks, store

ANS = {
    "t": {"choice": "design-agent", "band": "act", "is_escape": False, "group_band": "confirm"},
    "o": {"band": "no"},
}


@pytest.mark.parametrize("cond,expected", [
    ({"any": [{"judgment": "o", "band": "yes"}, {"judgment": "t", "band": "act"}]}, True),
    ({"any": [{"judgment": "o", "band": "yes"}, {"judgment": "t", "band": "confirm"}]}, False),
    ({"not": {"judgment": "o", "band": "yes"}}, True),
    ({"rule": "canva"}, True),
    ({"rule": "other"}, False),
    ({"judgment": "t", "band_in": ["act", "confirm"]}, True),
    ({"judgment": "t", "choice": "design-agent"}, True),
    ({"judgment": "t", "choice_in": ["code-agent"]}, False),
    ({"judgment": "t", "group_band_in": ["confirm"]}, True),
    ({"judgment": "t", "group_band": "act"}, False),
    ({"judgment": "missing", "band": "act"}, False),
    ({"judgment": "t"}, False),  # a judgment with no check never matches
    (None, True),
])
def test_decide_conditions(cond, expected):
    assert engine.condition(cond, {"canva"}, ANS) is expected


def test_shadow_block_is_logged_not_enforced(book, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev())
    book["mode"] = "shadow"
    book["hard_rules"].append({"id": "stop", "when": {"field": "prompt", "matches": "(?i)forbidden"}, "verdict": "block"})
    assert hooks.user_prompt(book, {"prompt": "this is forbidden", "session_id": "s", "cwd": "/p"}) is None
    assert store.read_log()[-1]["action"] == "would_block"


def test_enforce_block_stops_the_prompt(book, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev())
    book["hard_rules"].append({"id": "stop", "when": {"field": "prompt", "matches": "(?i)forbidden"},
                               "verdict": "block", "message": "no"})
    out = hooks.user_prompt(book, {"prompt": "this is forbidden", "session_id": "s", "cwd": "/p"})
    assert out["decision"] == "block" and "no" in out["reason"]


def test_cli_bench_and_log_print_reports(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev(confidence=0.95))
    rows = tmp_path / "rows.jsonl"
    rows.write_text("".join(json.dumps({"id": f"r{i}", "prompt": "fix my canva deck", "expect": "design-agent",
                                        "label_source": "reviewed"}) + "\n" for i in range(3)))
    assert cli.main(["bench", "agent-selection", "--file", str(rows)]) == 0
    out = capsys.readouterr().out
    assert "3 rows, accuracy 1.0" in out and "report:" in out
    hooks.user_prompt({**json.loads(json.dumps(_book())), "mode": "shadow"},
                      {"prompt": "fix my canva deck", "session_id": "s", "cwd": "/p"})
    hooks.pre_tool(_book(), {"tool_name": "Agent", "tool_input": {"subagent_type": "code-agent"}, "session_id": "s"})
    store.log({"hook": "user-prompt", "gate": "x", "action": "error", "error": "boom"})
    assert cli.main(["log", "--tail", "5"]) == 0
    out = capsys.readouterr().out
    assert "prompt" in out and "pre-tool" in out and "error  boom" in out


def _book():
    from conftest import BOOK
    return json.loads(json.dumps(BOOK))


def test_forbid_outranks_neutral(book):
    v = {"gate": "demo", "version": 3, "kind": "allow", "forbid": ["Explore"], "forbid_by": {"Explore": "r1"},
         "decided_by": "decide:main"}
    assert "forbidden" in hooks.violation(book, v, "Explore")
    assert hooks.violation(book, {**v, "forbid": []}, "Explore") is None


def test_bench_without_a_bench_file_says_so(capsys):
    assert cli.main(["bench", "agent-selection"]) == 2
    assert "has no bench file" in capsys.readouterr().err
