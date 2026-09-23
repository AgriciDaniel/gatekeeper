import pytest
from conftest import FakeJev

from gatekeeper import engine, roster
from gatekeeper.bands import confidence_band, noul_band
from gatekeeper.rules import match


def test_predicates():
    e = {"prompt": "Fix my Canva deck", "n": 3}
    assert match({"field": "prompt", "matches": r"(?i)canva"}, e)
    assert match({"field": "n", "in": [1, 3]}, e)
    assert match({"field": "prompt", "shorter_than": 40}, e)
    assert not match({"field": "prompt", "longer_than": 40}, e)
    assert match({"field": "missing", "exists": False}, e)
    assert not match({"field": "missing", "matches": "x"}, e)
    assert match({"all": [{"field": "n", "equals": 3}, {"not": {"field": "prompt", "matches": "rust"}}]}, e)
    assert match({"any": [{"field": "n", "equals": 9}, {"field": "n", "equals": 3}]}, e)


def test_band_edges_are_inclusive():
    b = {"act": 0.85, "confirm": 0.6}
    assert confidence_band(0.85, b) == "act"
    assert confidence_band(0.8499, b) == "confirm"
    assert confidence_band(0.6, b) == "confirm"
    assert confidence_band(0.59, b) == "escalate"
    n = {"yes": 0.7, "no": 0.3}
    assert (noul_band(0.7, n), noul_band(0.3, n), noul_band(0.5, n)) == ("yes", "no", "uncertain")


def test_roster_reads_agents_skills_plugins_and_dedupes():
    r = roster.build({})
    assert set(r) == {"design-agent", "code-agent", "blog", "blog-write", "canva-edit-design", "demo:helper"}
    assert r["code-agent"]["description"] == "Rust code and crates"
    assert r["blog-write"]["description"] == "Write a post"  # user copy wins over the plugin copy


def test_roster_exclude_and_keep(book):
    r = roster.build(book["roster"])
    assert "blog-write" not in r and "canva-edit-design" not in r
    assert "design-agent" in r


def test_candidates_narrow_the_choice_and_one_request_is_sent(book):
    jev = FakeJev()
    v = engine.evaluate(book, {"prompt": "fix slide 3 on my canva deck", "project": "x"}, client=jev)
    assert len(jev.calls) == 1
    asked = jev.calls[0]["questions"]
    assert set(asked["t"]["criteria"]) == {"design-agent", "main_thread"}
    assert asked["o"]["type"] == "noul"
    assert jev.calls[0]["state"] == {"prompt": "fix slide 3 on my canva deck", "project": "x"}
    assert (v.kind, v.target, v.decided_by) == ("route", "design-agent", "decide:route")
    assert v.message == "route to design-agent (1.0)"
    assert v.cost_usd > 0 and v.model == "jev-1.13.0"


def test_full_roster_when_no_candidate_rule_fires_and_forbid_removes(book):
    jev = FakeJev(choice="blog")
    engine.evaluate(book, {"prompt": "write something, no rust please"}, client=jev)
    options = set(jev.calls[0]["questions"]["t"]["criteria"])
    assert "code-agent" not in options
    assert {"blog", "design-agent", "demo:helper", "main_thread"} <= options


def test_stop_rule_short_circuits_without_calling_jev(book):
    jev = FakeJev()
    v = engine.evaluate(book, {"prompt": "/blog write"}, client=jev)
    assert jev.calls == [] and v.kind == "allow" and v.decided_by == "rule:cmd"


@pytest.mark.parametrize("choice,conf,noul,kind", [
    ("design-agent", 0.9, 0.03, "route"),
    ("design-agent", 0.7, 0.03, "suggest"),
    ("design-agent", 0.4, 0.03, "escalate"),
    ("design-agent", 0.95, 0.9, "confirm"),
    ("main_thread", 0.4, 0.9, "allow"),
])
def test_decision_table(book, choice, conf, noul, kind):
    v = engine.evaluate(book, {"prompt": "canva work"}, client=FakeJev(choice, conf, noul))
    assert v.kind == kind


def test_templates_render_answers(book):
    v = engine.evaluate(book, {"prompt": "canva work"}, client=FakeJev(confidence=0.4))
    assert v.message == "ask (main_thread 0.60, design-agent 0.40)"


def test_fail_open_and_fail_closed(book):
    v = engine.evaluate(book, {"prompt": "canva"}, client=FakeJev(error=TimeoutError("slow")))
    assert (v.kind, v.decided_by) == ("allow", "fail:open") and "slow" in v.error
    book["fail"] = "closed"
    v = engine.evaluate(book, {"prompt": "canva"}, client=FakeJev(error=TimeoutError("slow")))
    assert (v.kind, v.decided_by) == ("escalate", "fail:closed")


def test_too_many_options_fails_per_rulebook(book):
    book["judgments"]["t"]["options"] = {f"o{i}": None for i in range(300)}
    v = engine.evaluate(book, {"prompt": "anything"}, client=FakeJev())
    assert v.decided_by == "fail:open" and "255" in v.error
