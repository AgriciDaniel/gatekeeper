"""One regression test per finding from the 2026-09-22 adversarial review."""
import importlib.util
import json
import time

import pytest
from conftest import REPO, FakeJev

from gatekeeper import bench, cli, engine, hooks, jev, rulebook, store
from gatekeeper.rules import match

# The public starter, by path: a private gates/local override must never change what the suite tests.
REAL = rulebook.load(str(REPO / "gates" / "agent-selection.yaml"))


def _prompt(book, monkeypatch, text, fake, session="s1"):
    monkeypatch.setattr(engine.jev, "call", fake)
    return hooks.user_prompt(book, {"prompt": text, "cwd": "/x/proj", "session_id": session})


def _tool(book, name, tool="Agent", session="s1"):
    key = "skill" if tool == "Skill" else "subagent_type"
    return hooks.pre_tool(book, {"tool_name": tool, "tool_input": {key: name}, "session_id": session})


def _enforcing_real():
    book = json.loads(json.dumps(REAL))
    book["mode"] = "enforce"
    return book


# 1. forbid must not deny when the gate failed open
def test_forbid_does_not_deny_when_jev_is_down(book, monkeypatch):
    _prompt(book, monkeypatch, "notes, no rust please", FakeJev(error=TimeoutError("down")))
    assert _tool(book, "code-agent") is None
    assert store.read_log()[-1]["action"] == "allow"


# 2. a handler the user names is never denied
@pytest.mark.parametrize("prompt", ["ask the code-agent to review the client crate", "use code agent on this canva export"])
def test_named_handler_wins_over_the_route(book, monkeypatch, prompt):
    _prompt(book, monkeypatch, prompt + " canva", FakeJev(confidence=0.95))
    assert _tool(book, "code-agent") is None


def test_unnamed_handler_is_still_denied(book, monkeypatch):
    _prompt(book, monkeypatch, "fix my canva deck", FakeJev(confidence=0.95))
    assert _tool(book, "code-agent")["hookSpecificOutput"]["permissionDecision"] == "deny"


# 3. outside actions are flagged even for main-thread work, and short commands are judged
def test_outside_action_on_main_thread_asks_for_confirmation(monkeypatch):
    v = engine.evaluate(REAL, {"prompt": "force push master and delete the prod database now", "project": "x"},
                        client=FakeJev(choice="main_thread", confidence=0.9, noul=0.99))
    assert v.kind == "confirm" and v.target is None and "Confirm" in v.message and "Suggested" not in v.message


def test_outside_action_with_specialist_names_the_handler():
    v = engine.evaluate(REAL, {"prompt": "publish the canva deck to the team", "project": "x"},
                        client=FakeJev(choice="design-agent", confidence=0.95, noul=0.9))
    assert v.kind == "confirm" and v.target == "design-agent" and "design-agent" in v.message


@pytest.mark.parametrize("prompt,skipped", [("yes", True), ("go ahead!", True), ("continue", True),
                                            ("deploy to prod", False), ("delete it all", False)])
def test_only_pure_acknowledgements_skip_the_gate(prompt, skipped):
    fired = [r["id"] for r in REAL["hard_rules"] if match(r["when"], {"prompt": prompt})]
    assert ("short-reply" in fired) == skipped


def test_other_specialists_are_still_denied_under_a_route():
    v = {"gate": "agent-selection", "version": 3, "kind": "route", "target": "design-agent", "decided_by": "decide:route"}
    assert hooks.violation(REAL, v, "code-agent")


# 4 and 5 (cross-family helpers, candidate-rule regexes) test a personal rulebook's
# rules; they live next to that rulebook in gates/local/ and run with `pytest gates/local`.


# 6. installer: exact gate match, shared groups, stable when already installed
spec = importlib.util.spec_from_file_location("install_hooks", REPO / "scripts" / "install_hooks.py")
install_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_hooks)


def test_installer_does_not_touch_other_gates_or_shared_groups():
    ours_v2 = install_hooks.entries("agent-selection-v2")
    other = {"type": "command", "command": "/other-tool.sh"}
    shared = {"matcher": "Agent|Task|Skill", "hooks": [other, ours_v2["PreToolUse"]["hooks"][0]]}
    settings = {"hooks": {"PreToolUse": [shared]}}
    installed = install_hooks.merge(settings, "agent-selection", uninstall=False)
    assert installed["hooks"]["PreToolUse"][0] == shared  # v2 and the other tool untouched
    removed = install_hooks.merge(installed, "agent-selection", uninstall=True)
    assert removed == settings
    only_other = install_hooks.merge(settings, "agent-selection-v2", uninstall=True)
    assert only_other["hooks"]["PreToolUse"] == [{"matcher": "Agent|Task|Skill", "hooks": [other]}]


def test_installer_leaves_an_installed_file_unchanged():
    once = install_hooks.merge({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/a.sh"}]}]}},
                               "agent-selection", uninstall=False)
    once["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "/b.sh"}]})
    assert install_hooks.merge(once, "agent-selection", uninstall=False) == once


# 7. unquoted yes/no in decide, and lint of band names
def test_unquoted_band_yes_in_decide_is_coerced_and_bad_bands_are_linted(tmp_path):
    text = rulebook.resolve("agent-selection").read_text()
    path = tmp_path / "g.yaml"
    path.write_text(text.replace('band: "yes"', "band: yes"))
    book = rulebook.load(str(path))
    assert book["decide"][0]["when"]["all"][0]["band"] == "yes"
    path.write_text(text.replace("when: {judgment: target, band: act}", "when: {judgment: target, band: yes}"))
    assert cli.main(["lint", str(path)]) == 1


# 8. suggested threshold ignores escape picks
def test_bench_threshold_counts_specialist_picks_only(book):
    rows = [{"id": str(i), "prompt": "canva x", "expect": "code-agent"} for i in range(3)]
    report = bench.run(book, rows, client=FakeJev(choice="main_thread", confidence=0.99))
    assert report["suggested_act_threshold"] is None
    assert all(r["escape"] for r in report["rows"])


# 9. a missing template field renders empty instead of raw braces
def test_missing_template_field_renders_empty(book):
    book["decide"][2]["message"] = "route to {t.choice} ({t.nope:.2f}) {missing.value}"
    v = engine.evaluate(book, {"prompt": "canva"}, client=FakeJev(confidence=0.95))
    assert v.message == "route to design-agent () " and "{" not in v.message


# Suspicions from the review
def test_new_prompt_clears_the_old_verdict_even_if_judging_crashes(book, monkeypatch, tmp_path):
    _prompt(book, monkeypatch, "fix my canva deck", FakeJev(confidence=0.95))
    assert _tool(book, "code-agent") is not None  # old route enforced

    def boom(*a, **k):
        raise RuntimeError("crash mid-hook")

    monkeypatch.setattr(engine, "evaluate", boom)
    path = tmp_path / "g.yaml"
    import yaml
    path.write_text(yaml.safe_dump(book))
    assert hooks.run("user-prompt", str(path), json.dumps({"prompt": "something new", "session_id": "s1"})) == ""
    assert _tool(book, "code-agent") is None  # stale verdict did not survive


def test_verdict_expires(book, monkeypatch):
    _prompt(book, monkeypatch, "fix my canva deck", FakeJev(confidence=0.95))
    monkeypatch.setattr(store.time, "time", lambda: time.monotonic() + 1e10)
    assert _tool(book, "code-agent") is None


def test_missing_session_id_never_enforces(book, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev(confidence=0.95))
    hooks.user_prompt(book, {"prompt": "fix my canva deck", "cwd": "/x"})
    assert hooks.pre_tool(book, {"tool_name": "Agent", "tool_input": {"subagent_type": "code-agent"}}) is None
    assert not (store.home() / "state").exists() or not list((store.home() / "state").iterdir())


def test_sessions_do_not_share_verdicts(book, monkeypatch):
    _prompt(book, monkeypatch, "fix my canva deck", FakeJev(confidence=0.95), session="a")
    assert _tool(book, "code-agent", session="b") is None


def test_secrets_are_redacted_before_jev_and_the_log(book, monkeypatch):
    fake = FakeJev(confidence=0.95)
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"
    _prompt(book, monkeypatch, f"my key is {secret} fix the canva deck", fake)
    assert secret not in json.dumps(fake.calls) and "[redacted]" in fake.calls[0]["state"]["prompt"]
    assert secret not in json.dumps(store.read_log())


def test_hung_client_is_abandoned_at_the_deadline(book):
    def hang(*a, **k):
        time.sleep(5)

    book["timeout_s"] = 0.3
    start = time.monotonic()
    v = engine.evaluate(book, {"prompt": "canva"}, client=hang)
    assert v.decided_by == "fail:open" and time.monotonic() - start < 1.5


@pytest.mark.parametrize("line", ["export TYPESAFE_API_KEY=abc123", "TYPESAFE_API_KEY=abc123  # prod key",
                                  "TYPESAFE_API_KEY='abc123'", 'TYPESAFE_API_KEY="abc123" # x'])
def test_env_file_variants(monkeypatch, tmp_path, line):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(line + "\n")
    monkeypatch.setenv("GATEKEEPER_ENV_FILE", str(env))
    assert jev.load_key() == "abc123"


def test_forbid_message_names_only_the_rule_that_forbade(book, monkeypatch):
    book["hard_rules"].append({"id": "also-canva", "when": {"field": "prompt", "matches": "canva"}, "candidates": ["design-agent"]})
    _prompt(book, monkeypatch, "canva deck, no rust", FakeJev(choice="main_thread", confidence=0.9))
    reason = _tool(book, "code-agent")["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.endswith("forbidden for this request by rule no-rust.")


# Found in the live session test: background agent results arrive through UserPromptSubmit
@pytest.mark.parametrize("harness", [
    '<agent-message from="a1"> [Subagent hand-back] The report mentions code-agent and seo-agent',
    "<task-notification><task-id>x</task-id></task-notification>",
    "[SYSTEM NOTIFICATION - NOT USER INPUT] something",
])
def test_harness_messages_do_not_rejudge_or_reroute_the_turn(monkeypatch, harness):
    book = _enforcing_real()
    fake = FakeJev(choice="design-agent", confidence=0.95)
    _prompt(book, monkeypatch, "fix the text on slide 3 of my canva deck", fake)
    calls = len(fake.calls)
    assert _prompt(book, monkeypatch, harness, fake) is None
    assert len(fake.calls) == calls  # not judged
    denied = _tool(book, "code-agent")  # original route still enforced, report did not "name" it
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.read_log()[-2]["action"] == "passthrough"


def test_a_real_prompt_that_mentions_tags_is_still_judged(monkeypatch):
    book = _enforcing_real()
    fake = FakeJev(choice="main_thread", confidence=0.9)
    _prompt(book, monkeypatch, "why does my <agent-message> parser fail on nested tags?", fake)
    assert len(fake.calls) == 1
