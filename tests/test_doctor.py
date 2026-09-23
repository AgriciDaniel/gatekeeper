import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import REPO

from gatekeeper import cli, doctor, rulebook, store

REAL = rulebook.load(str(REPO / "gates" / "agent-selection.yaml"))  # never a private override


@pytest.fixture(autouse=True)
def no_repo_hooks(monkeypatch, tmp_path):
    """Point the repo's own settings file somewhere empty, so tests control every hook location."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(doctor.rulebook, "REPO", fake_repo)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")


def by_name(checks, name):
    return next(c for c in checks if c.name == name)


def book(**over):
    b = json.loads(json.dumps(REAL))
    b.update(over)
    return b


# ---------- model ----------

class Client:
    def __init__(self, served="jev-1.13.0", latest=None, fail=False, delay=0.0):
        self.served, self.latest, self.fail, self.delay = served, latest or served, fail, delay

    def __call__(self, state, questions, model, timeout_s):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("404 model not found")
        return {"model": self.latest if model == "jev-latest" else self.served, "answers": {"probe": {"noul": .9}}}


def test_model_healthy():
    checks = doctor.check_model(book(), Client())
    assert [c.status for c in checks] == ["ok", "ok", "ok"]


def test_retired_pin_fails():
    checks = doctor.check_model(book(), Client(fail=True))
    assert checks[0].status == "fail" and "did not answer" in checks[0].detail and "re-bench" in checks[0].fix


def test_served_version_differs_from_pin_warns():
    assert doctor.check_model(book(), Client(served="jev-1.14.0"))[0].status == "warn"


def test_newer_latest_is_info():
    c = by_name(doctor.check_model(book(), Client(latest="jev-1.14.0")), "model-latest")
    assert c.status == "info" and "jev-1.14.0" in c.detail


def test_slow_model_warns_on_latency():
    c = by_name(doctor.check_model(book(timeout_s=0.2), Client(delay=0.18)), "latency")
    assert c.status == "warn"


def test_missing_key_fails_and_skips_model(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setenv("GATEKEEPER_ENV_FILE", str(tmp_path / "none"))
    checks = doctor.run("agent-selection", client=Client())
    assert by_name(checks, "api-key").status == "fail"
    assert not any(c.name == "model" for c in checks)
    assert doctor.exit_code(checks) == 1


# ---------- hooks ----------

def _install(settings_path, gate="agent-selection", bin_path=None):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ih", REPO / "scripts" / "install_hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    data = ih.merge({}, gate, uninstall=False)
    if bin_path:
        text = json.dumps(data).replace(str(ih.BIN), bin_path)
        data = json.loads(text)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data))
    return data


def test_hooks_not_installed_warns():
    c = doctor.check_hooks(book())
    assert c.status == "warn" and "never runs" in c.detail


def test_hooks_installed_in_a_project(tmp_path):
    _install(tmp_path / "proj" / ".claude" / "settings.json")
    c = doctor.check_hooks(book(), [str(tmp_path / "proj")])
    assert c.status == "ok" and "proj" in c.detail


def test_hooks_pointing_at_a_missing_binary_fail(tmp_path):
    _install(tmp_path / "proj" / ".claude" / "settings.json", bin_path="/gone/bin/gatekeeper")
    assert doctor.check_hooks(book(), [str(tmp_path / "proj")]).status == "fail"


def test_half_installed_hooks_warn(tmp_path):
    path = tmp_path / "proj" / ".claude" / "settings.json"
    data = _install(path)
    del data["hooks"]["PreToolUse"]
    path.write_text(json.dumps(data))
    c = doctor.check_hooks(book(), [str(tmp_path / "proj")])
    assert c.status == "warn" and "PreToolUse" in c.detail


def test_hooks_for_another_gate_do_not_count(tmp_path):
    _install(tmp_path / "proj" / ".claude" / "settings.json", gate="email-triage")
    assert doctor.check_hooks(book(), [str(tmp_path / "proj")]).status == "warn"


# ---------- activity and passthrough ----------

def _row(decided_by="decide:route", excerpt="fix my canva deck", days_ago=0, **extra):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    row = {"ts": ts, "hook": "UserPromptSubmit", "gate": "agent-selection", "action": "none",
           "prompt_excerpt": excerpt, "verdict": {"decided_by": decided_by, "error": "JevError: 503" if decided_by.startswith("fail") else None}}
    row.update(extra)
    return row


@pytest.mark.parametrize("fails,status", [(0, "ok"), (1, "warn"), (6, "fail")])
def test_fail_open_rate(fails, status):
    rows = [_row() for _ in range(10 - fails)] + [_row("fail:open") for _ in range(fails)]
    c = doctor.check_activity(book(), rows, 7, hooks_installed=True)[0]
    assert c.status == status
    if fails:
        assert "503" in c.detail


def test_stop_rule_prompts_are_not_counted_as_judged():
    rows = [_row("rule:short-reply") for _ in range(20)]
    assert "too few" in doctor.check_activity(book(), rows, 7, True)[0].detail


def test_installed_but_silent_warns_and_old_rows_are_ignored():
    rows = [_row(days_ago=30) for _ in range(10)]
    assert doctor.check_activity(book(), rows, 7, hooks_installed=True)[0].status == "warn"
    assert doctor.check_activity(book(), rows, 7, hooks_installed=False)[0].status == "info"


def test_hook_errors_warn():
    rows = [_row() for _ in range(6)] + [{"ts": datetime.now(timezone.utc).isoformat(), "hook": "user-prompt", "action": "error", "error": "KeyError: x"}]
    assert by_name(doctor.check_activity(book(), rows, 7, True), "hook-errors").status == "warn"


def test_harness_message_that_was_judged_is_flagged():
    rows = [_row(excerpt='<new-harness-tag from="x"> report')]
    c = doctor.check_passthrough(book(), rows, 7)
    assert c.status == "warn" and "passthrough" in c.fix
    assert doctor.check_passthrough(book(), [_row()], 7).status == "ok"


# ---------- roster and bench ----------

def test_roster_reports_uncovered_handlers():
    b = book(families={}, neutral=[], hard_rules=[])
    c = doctor.check_roster(b)
    assert c.status == "info" and "design-agent" in c.detail
    b = book(neutral=["*"])
    assert doctor.check_roster(b).status == "ok"


def _bench(version=5, model="jev-1.13.0", days_ago=0, labels=("reviewed",)):
    path = store.home() / "bench"
    path.mkdir(parents=True, exist_ok=True)
    ran = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    (path / f"agent-selection-{ran.replace(':', '')}.json").write_text(json.dumps(
        {"ran_at": ran, "version": version, "model": model, "accuracy": .82, "label_sources": list(labels)}))


def test_bench_never_run_warns():
    assert doctor.check_bench(book()).status == "warn"


def test_bench_current_and_reviewed_is_ok():
    _bench(version=REAL["version"])
    assert doctor.check_bench(book()).status == "ok"


@pytest.mark.parametrize("kw,text", [({"version": 99}, "last bench was v99"), ({"model": "jev-1.0.0"}, "last bench used"),
                                     ({"days_ago": 45}, "45 days old"), ({"labels": ("claude-draft",)}, "unreviewed")])
def test_bench_staleness(kw, text):
    _bench(**{"version": REAL["version"], **kw})
    c = doctor.check_bench(book())
    assert c.status == "warn" and text in c.detail


# ---------- runner and CLI ----------

def test_unknown_gate_fails_cleanly():
    checks = doctor.run("no-such-gate")
    assert checks[0].status == "fail" and doctor.exit_code(checks) == 1


def test_offline_run_never_calls_the_model():
    def boom(*a, **k):
        raise AssertionError("network used")

    checks = doctor.run("agent-selection", offline=True, client=boom)
    assert by_name(checks, "model").detail == "skipped (--offline)"


def test_render_and_json():
    checks = [doctor.Check("a", "ok", "fine"), doctor.Check("b", "fail", "broken", "do x")]
    text = doctor.render(checks)
    assert "[FAIL] b" in text and "fix: do x" in text and text.rstrip().endswith("0 warn, 1 fail")
    data = json.loads(doctor.to_json(checks))
    assert data["healthy"] is False and data["checks"][1]["fix"] == "do x"


def test_cli_doctor_offline_json(capsys):
    code = cli.main(["doctor", "--offline", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == (0 if data["healthy"] else 1)
    assert {c["name"] for c in data["checks"]} >= {"rulebook", "api-key", "hooks", "activity", "roster", "bench"}
