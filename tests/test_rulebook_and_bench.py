import shutil

from conftest import FakeJev

from gatekeeper import bench, cli, rulebook


def test_shipped_rulebooks_validate():
    for path in rulebook.GATES.glob("*.yaml"):
        book = rulebook.load(str(path))
        errors, _ = cli.lint(book)
        assert errors == [], (path.name, errors)


def test_set_mode_keeps_comments(tmp_path):
    copy = tmp_path / "agent-selection.yaml"
    shutil.copy(rulebook.resolve("agent-selection"), copy)
    rulebook.set_mode(str(copy), "enforce")
    text = copy.read_text()
    assert '\nmode: "enforce"' in text and "# Gatekeeper starter rulebook" in text
    assert rulebook.load(str(copy))["mode"] == "enforce"


def test_bench_scores_by_family_and_suggests_threshold(book):
    rows = [
        {"id": "a", "prompt": "canva deck", "expect": "design-agent"},
        {"id": "b", "prompt": "canva slide", "expect": "canva-edit-design"},  # same family counts
        {"id": "c", "prompt": "/blog", "expect": "main_thread"},  # stop rule -> escape
        {"id": "d", "prompt": "canva thing", "expect": "code-agent"},  # a miss
    ]
    report = bench.run(book, rows, client=FakeJev(confidence=0.9))
    assert report["n"] == 4 and report["accuracy"] == 0.75
    assert [m["id"] for m in report["misses"]] == ["d"]
    assert report["bands"]["act"] == {"n": 3, "accuracy": 0.667}
    assert report["suggested_act_threshold"] is None  # 0.667 < 0.9 target


def test_unquoted_yaml_yes_no_keys_are_coerced(tmp_path):
    text = rulebook.resolve("agent-selection").read_text().replace('{"yes": 0.70, "no": 0.30}', "{yes: 0.70, no: 0.30}")
    text = text.replace('"true": The request', "true: The request").replace('"false": The request', "false: The request")
    path = tmp_path / "unquoted.yaml"
    path.write_text(text)
    spec = rulebook.load(str(path))["judgments"]["outside_action"]
    assert spec["bands"] == {"yes": 0.70, "no": 0.30}
    assert set(spec["criteria"]) == {"true", "false"}


def test_unquoted_mode_off_is_really_off(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text(rulebook.resolve("agent-selection").read_text().replace("mode: shadow", "mode: off"))
    assert rulebook.load(str(path))["mode"] == "off"
    rulebook.set_mode(str(path), "shadow")
    rulebook.set_mode(str(path), "off")  # idempotent on an already quoted value
    assert rulebook.load(str(path))["mode"] == "off"
    assert path.read_text().count("\nmode:") == 1


def test_mode_off_hook_never_calls_jev(tmp_path, monkeypatch):
    from conftest import FakeJev
    from gatekeeper import engine, hooks
    path = tmp_path / "g.yaml"
    path.write_text(rulebook.resolve("agent-selection").read_text().replace("mode: shadow", "mode: off"))
    jev = FakeJev()
    monkeypatch.setattr(engine.jev, "call", jev)
    assert hooks.run("user-prompt", str(path), '{"prompt": "fix my canva deck please", "session_id": "m"}') == ""
    assert jev.calls == []
