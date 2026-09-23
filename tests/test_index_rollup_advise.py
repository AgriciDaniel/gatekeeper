"""v0.2: index rosters, rollup by group, advise mode, local overrides, per-gate state."""
import importlib.util
import json

from conftest import REPO, FakeJev

from gatekeeper import cli, doctor, engine, hooks, jev, roster, rulebook, store

EXAMPLE = REPO / "examples" / "marketing-team" / "marketing-team.yaml"


class GroupJev:
    """Answers the Choice with the given probabilities, like the real API."""

    def __init__(self, probabilities, confidence=None):
        self.probabilities = probabilities
        self.confidence = confidence

    def __call__(self, state, questions, model, timeout_s):
        top = max(self.probabilities, key=self.probabilities.get)
        answers = {}
        for qid, q in questions.items():
            assert set(self.probabilities) <= set(q["criteria"]), "asked about an option that was not offered"
            answers[qid] = {"type": "choice", "choice": top,
                            "confidence": self.confidence if self.confidence is not None else self.probabilities[top],
                            "probabilities": self.probabilities}
        return {"model": model, "answers": answers, "usage": {"input_tokens": 100}}


def test_index_roster_reads_team_json():
    book = rulebook.load(str(EXAMPLE))
    r = roster.build(book["roster"])
    assert len(r) == 24
    e = r["keyword-research"]
    assert e["group"] == "seo" and e["path"] == "skills/keyword-research/SKILL.md"
    assert e["description"].startswith("Find and cluster") and "Choosing topics" in e["description"]  # description + use_when


def test_index_where_filter_and_list_file(tmp_path):
    f = tmp_path / "idx.json"
    f.write_text(json.dumps([{"id": "a", "status": "active", "description": "A"},
                             {"id": "b", "status": "retired", "description": "B"}]))
    r = roster.build({"sources": ["index"], "index": {"file": str(f), "where": {"status": "active"}}})
    assert list(r) == ["a"]
    assert roster.build({"sources": ["index"], "index": {"file": str(tmp_path / "missing.json")}}) == {}


def test_rollup_routes_to_role_when_skill_is_unsure():
    book = rulebook.load(str(EXAMPLE))
    client = GroupJev({"technical-seo-audit": 0.5, "keyword-research": 0.45, "blog-post": 0.05}, confidence=0.5)
    v = engine.evaluate(book, {"prompt": "help with seo", "project": "x"}, client=client)
    ans = v.answers["skill"]
    assert ans["group_choice"] == "seo" and ans["group_confidence"] == 0.95 and ans["group_band"] == "act"
    assert (v.kind, v.target, v.decided_by) == ("suggest", "seo", "decide:role")
    assert "this is seo work" in v.message


def test_confident_skill_routes_with_path():
    book = rulebook.load(str(EXAMPLE))
    v = engine.evaluate(book, {"prompt": "audit my ad account", "project": "x"},
                        client=GroupJev({"ad-account-audit": 0.97, "search-ads": 0.03}))
    assert (v.kind, v.target) == ("route", "ad-account-audit")
    assert "Read `skills/ad-account-audit/SKILL.md`" in v.message


def test_escape_is_its_own_group():
    book = rulebook.load(str(EXAMPLE))
    v = engine.evaluate(book, {"prompt": "rename a variable", "project": "x"},
                        client=GroupJev({"not_marketing": 0.99, "blog-post": 0.01}))
    assert v.kind == "allow" and v.answers["skill"]["group_choice"] == "not_marketing"


def test_lint_rejects_rollup_without_groups_and_missing_index(tmp_path):
    text = EXAMPLE.read_text()
    no_group = tmp_path / "no-group.yaml"
    no_group.write_text(text.replace("    group: role\n", "").replace("file: team.json", f"file: {EXAMPLE.parent / 'team.json'}"))
    assert cli.main(["lint", str(no_group)]) == 1
    missing = tmp_path / "missing.yaml"
    missing.write_text(text.replace("file: team.json", "file: nope.json"))
    assert cli.main(["lint", str(missing)]) == 1
    assert cli.main(["lint", str(EXAMPLE)]) == 0


def test_example_gate_resolves_by_name():
    assert rulebook.resolve("marketing-team") == EXAMPLE


def test_local_override_wins(tmp_path, monkeypatch):
    local = tmp_path / "local-gates"
    local.mkdir(exist_ok=True)
    mine = local / "agent-selection.yaml"
    mine.write_text((REPO / "gates" / "agent-selection.yaml").read_text())
    assert rulebook.resolve("agent-selection") == mine
    mine.unlink()
    assert rulebook.resolve("agent-selection") == REPO / "gates" / "agent-selection.yaml"


def test_advise_injects_but_never_blocks_or_denies(book, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev(confidence=0.95))
    book["mode"] = "advise"
    book["hard_rules"].append({"id": "stop", "when": {"field": "prompt", "matches": "(?i)forbidden"},
                               "verdict": "block", "message": "not allowed"})
    out = hooks.user_prompt(book, {"prompt": "fix my canva deck", "session_id": "s1", "cwd": "/tmp/p"})
    assert out["hookSpecificOutput"]["additionalContext"].startswith("Gatekeeper (demo v3)")
    assert hooks.pre_tool(book, {"tool_name": "Agent", "tool_input": {"subagent_type": "code-agent"},
                                 "session_id": "s1"}) is None
    out = hooks.user_prompt(book, {"prompt": "this is forbidden", "session_id": "s1", "cwd": "/tmp/p"})
    assert "decision" not in (out or {}) and "not allowed" in out["hookSpecificOutput"]["additionalContext"]


def test_two_gates_keep_separate_verdicts(book):
    other = json.loads(json.dumps(book))
    other["gate"] = "other"
    store.save_verdict("s", {"gate": "demo", "kind": "route"})
    store.save_verdict("s", {"gate": "other", "kind": "allow"})
    assert store.load_verdict("s", "demo")["kind"] == "route"
    assert store.load_verdict("s", "other")["kind"] == "allow"
    store.clear_verdict("s", "other")
    assert store.load_verdict("s", "demo") is not None and store.load_verdict("s", "other") is None


def test_key_lookup_order(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = tmp_path / "config" / "gatekeeper"
    config.mkdir(parents=True)
    (config / "env").write_text("TYPESAFE_API_KEY=from-config\n")
    assert jev.load_key() == "from-config"
    named = tmp_path / "named.env"
    named.write_text("TYPESAFE_API_KEY=from-named\n")
    monkeypatch.setenv("GATEKEEPER_ENV_FILE", str(named))
    assert jev.load_key() == "from-named"
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    assert jev.load_key() == "from-env"


def test_doctor_flags_broken_skill_links(isolated):
    skills = isolated / "claude" / "skills"
    (skills / "moved").symlink_to(isolated / "gone", target_is_directory=True)
    c = doctor.check_skills()
    assert c.status == "warn" and "moved" in c.detail  # someone else's skill: warn, not fail
    (skills / "moved").unlink()
    (skills / "gatekeeper").symlink_to(isolated / "gone", target_is_directory=True)
    assert doctor.check_skills().status == "fail"  # our own link broken: the skill is gone
    (skills / "gatekeeper").unlink()
    assert doctor.check_skills().status == "info"  # gatekeeper skill not installed in the fake ~/.claude
    (skills / "gatekeeper").symlink_to(REPO, target_is_directory=True)
    assert doctor.check_skills().status == "ok"


def test_installer_quotes_gate_paths():
    spec = importlib.util.spec_from_file_location("ih", REPO / "scripts" / "install_hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    gate = "/home/me/My Vault/gates/routing.yaml"
    merged = ih.merge({}, gate, uninstall=False)
    hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert "'/home/me/My Vault/gates/routing.yaml'" in hook["command"] and ih.is_ours(hook, gate)
    assert ih.merge(merged, gate, uninstall=True) == {}
