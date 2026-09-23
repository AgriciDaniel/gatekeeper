import json

from conftest import FakeJev

from gatekeeper import engine, hooks, store


def _prompt(book, monkeypatch, text, jev):
    monkeypatch.setattr(engine.jev, "call", jev)
    return hooks.user_prompt(book, {"prompt": text, "cwd": "/home/x/proj", "session_id": "s1"})


def _tool(book, tool, name):
    key = "skill" if tool == "Skill" else "subagent_type"
    return hooks.pre_tool(book, {"tool_name": tool, "tool_input": {key: name}, "session_id": "s1"})


def test_enforce_injects_route_and_denies_other_specialists(book, monkeypatch):
    out = _prompt(book, monkeypatch, "fix slide 3 on my canva deck", FakeJev(confidence=0.95))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Gatekeeper (demo v3): route to design-agent")

    denied = _tool(book, "Agent", "code-agent")["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny" and "design-agent" in denied["permissionDecisionReason"]
    assert _tool(book, "Agent", "design-agent") is None
    assert _tool(book, "Skill", "canva-edit-design") is None  # same family
    assert _tool(book, "Agent", "Explore") is None  # neutral


def test_suggest_band_never_blocks(book, monkeypatch):
    _prompt(book, monkeypatch, "canva deck please", FakeJev(confidence=0.7))
    assert _tool(book, "Agent", "code-agent") is None


def test_forbid_blocks_even_without_route(book, monkeypatch):
    _prompt(book, monkeypatch, "blog post, no rust", FakeJev(choice="main_thread", confidence=0.9))
    denied = _tool(book, "Agent", "code-agent")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shadow_mode_outputs_nothing_but_logs_would_actions(book, monkeypatch):
    book["mode"] = "shadow"
    assert _prompt(book, monkeypatch, "fix my canva deck", FakeJev(confidence=0.95)) is None
    assert _tool(book, "Agent", "code-agent") is None
    actions = [r["action"] for r in store.read_log()]
    assert actions == ["would_inject", "would_deny"]
    first = store.read_log()[0]
    assert "prompt" not in first and len(first["prompt_sha256"]) == 64


def test_block_verdict_blocks_the_prompt(book, monkeypatch):
    book["hard_rules"].insert(0, {"id": "stop", "when": {"field": "prompt", "matches": "rm -rf"}, "verdict": "block",
                                  "message": "never"})
    out = _prompt(book, monkeypatch, "please rm -rf the vault", FakeJev())
    assert out == {"decision": "block", "reason": "Gatekeeper (demo v3): never"}


def test_run_kill_switch_and_error_safety(book, monkeypatch, tmp_path):
    path = tmp_path / "demo.yaml"
    import yaml
    path.write_text(yaml.safe_dump(book))
    monkeypatch.setattr(engine.jev, "call", FakeJev(confidence=0.95))
    payload = json.dumps({"prompt": "fix my canva deck", "cwd": "/x", "session_id": "s2"})
    assert "additionalContext" in hooks.run("user-prompt", str(path), payload)
    monkeypatch.setenv("GATEKEEPER", "off")
    assert hooks.run("user-prompt", str(path), payload) == ""
    monkeypatch.delenv("GATEKEEPER")
    assert hooks.run("user-prompt", str(path), "not json") == ""
    assert store.read_log()[-1]["action"] == "error"


def test_tool_call_without_a_verdict_is_allowed(book):
    assert _tool(book, "Agent", "code-agent") is None
