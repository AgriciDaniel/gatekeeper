"""One regression test per finding from the 2026-09-23 review of v0.2."""
import importlib.util
import json
import os
import subprocess
import sys
import time

from conftest import REPO

from gatekeeper import cli, doctor, roster, rulebook, store

EXAMPLE = REPO / "examples" / "marketing-team" / "marketing-team.yaml"
spec = importlib.util.spec_from_file_location("ih_v02", REPO / "scripts" / "install_hooks.py")
ih = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ih)


# 2. doctor finds hooks installed by rulebook path
def test_doctor_sees_hooks_installed_by_path(tmp_path):
    project = tmp_path / "proj"
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(ih.merge({}, str(EXAMPLE), uninstall=False)))
    c = doctor.check_hooks(rulebook.load("marketing-team"), [str(project)])
    assert c.status == "ok" and str(project) in c.detail


# 3. the hook command names the installing interpreter, quoted; a missing dependency never breaks a session
def test_hook_command_uses_installing_python():
    cmd = ih.entries("agent-selection")["UserPromptSubmit"]["hooks"][0]["command"]
    assert cmd.startswith(ih.shlex.quote(sys.executable) + " ")


def test_doctor_fails_on_an_interpreter_without_dependencies(tmp_path):
    fake = tmp_path / "python-without-deps"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    assert "lacks PyYAML" in doctor._interpreter_problem(str(fake))
    assert doctor._interpreter_problem(sys.executable) is None


def test_hook_exits_zero_when_dependencies_are_missing(tmp_path):
    shadow = tmp_path / "shadow"
    (shadow / "yaml").mkdir(parents=True)
    (shadow / "yaml" / "__init__.py").write_text("raise ImportError('no yaml here')\n")
    env = {**os.environ, "PYTHONPATH": str(shadow), "GATEKEEPER_HOME": str(tmp_path / "gk")}
    done = subprocess.run([sys.executable, str(REPO / "bin" / "gatekeeper"), "hook", "user-prompt", "--gate", "agent-selection"],
                          input='{"prompt": "hi"}', capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0 and done.stdout == ""


# 4. malformed index files never crash lint or doctor
def test_malformed_index_is_reported_not_raised(tmp_path):
    for content in ['"oops"', "42", "not json", b"\xff\xfe"]:
        f = tmp_path / "idx.json"
        f.write_bytes(content if isinstance(content, bytes) else content.encode())
        assert roster.build({"sources": ["index"], "index": {"file": str(f)}}) == {}
    f.write_text(json.dumps({"entries": [{"id": "a", "summary": "Alpha"}]}))
    r = roster.build({"sources": ["index"], "index": {"file": str(f), "description": "summary"}})
    assert r["a"]["description"] == "Alpha" and r["a"]["kind"] == "entry"


# 5. a gate installed by name and by path is one gate
def test_name_and_path_install_once():
    by_name = ih.merge({}, "marketing-team", uninstall=False)
    both = ih.merge(by_name, str(EXAMPLE), uninstall=False)
    assert len(both["hooks"]["UserPromptSubmit"]) == 1 and len(both["hooks"]["PreToolUse"]) == 1
    assert ih.merge(both, "marketing-team", uninstall=True) == {}


# 7. `mode` never edits a shipped rulebook
def test_mode_on_a_shipped_gate_is_saved_locally():
    before = EXAMPLE.read_text()
    assert cli.main(["mode", "marketing-team", "shadow"]) == 0
    assert EXAMPLE.read_text() == before
    assert rulebook.load("marketing-team")["mode"] == "shadow"
    assert json.loads((rulebook.local_dir() / "modes.json").read_text()) == {str(EXAMPLE.resolve()): "shadow"}


# 8. rollup lint gaps
def test_lint_rejects_escape_named_group_and_group_band_without_rollup(tmp_path):
    team = json.loads((EXAMPLE.parent / "team.json").read_text())
    team["skills"][0]["role"] = "not_marketing"
    (tmp_path / "team.json").write_text(json.dumps(team))
    clash = tmp_path / "clash.yaml"
    clash.write_text(EXAMPLE.read_text())
    assert cli.main(["lint", str(clash)]) == 1
    no_rollup = tmp_path / "no-rollup.yaml"
    no_rollup.write_text(EXAMPLE.read_text().replace("    rollup: group\n", "")
                         .replace("file: team.json", f"file: {EXAMPLE.parent / 'team.json'}"))
    assert cli.main(["lint", str(no_rollup)]) == 1


# 11. stale and v0.1 state files are pruned
def test_old_state_files_are_pruned():
    state = store.home() / "state"
    state.mkdir(parents=True)
    (state / "legacy-session.json").write_text("{}")
    store.save_verdict("old", {"gate": "demo", "kind": "allow"})
    old = state / "demo" / "old.json"
    os.utime(old, (time.time() - store.STATE_TTL_S - 60,) * 2)
    store.save_verdict("new", {"gate": "demo", "kind": "allow"})
    assert not (state / "legacy-session.json").exists() and not old.exists()
    assert store.load_verdict("new", "demo") is not None



def test_home_option_gives_hooks_their_own_log_and_stays_recognized(tmp_path):
    home = str(tmp_path / "demo home")
    merged = ih.merge({}, "marketing-team", uninstall=False, home=home)
    hook = merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hook["command"].startswith(f"env GATEKEEPER_HOME='{home}' ") and ih.is_ours(hook, "marketing-team")
    assert doctor._python_of(ih.shlex.split(hook["command"])) == sys.executable
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(json.dumps(merged))
    assert doctor.check_hooks(rulebook.load("marketing-team"), [str(project)]).status == "ok"
    env = {**os.environ}
    env.pop("GATEKEEPER_HOME", None)
    subprocess.run(["sh", "-c", hook["command"]], input='{"prompt": "hi", "session_id": "d"}', text=True, env=env, timeout=30)
    assert list((tmp_path / "demo home" / "log").glob("*.jsonl"))
    assert ih.merge(merged, "marketing-team", uninstall=True) == {}
