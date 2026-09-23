import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import REPO, FakeJev

from gatekeeper import cli, engine, jev, rulebook, store

KEY = "sk-test-SECRET-value-1234567890"
OK = {"model": "jev-1.13.0", "answers": {"o": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 10}}


class Server:
    """Local stand-in for the TypeSafe API. `script` is a list of (status, body, delay_s)."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.seen.append({"auth": self.headers.get("Authorization"),
                                   "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                status, body, delay = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                time.sleep(delay)
                data = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    if status in (429, 503):
                        self.send_header("retry-after", "0.05")
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/systemone"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def api(monkeypatch):
    servers = []

    def make(*script):
        s = Server(script)
        servers.append(s)
        monkeypatch.setattr(jev, "API", s.url)
        return s

    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    yield make
    for s in servers:
        s.close()


def test_call_sends_one_request_with_bearer_key(api):
    s = api((200, OK, 0))
    out = jev.call({"a": 1}, {"o": {"type": "noul", "instructions": "?"}}, "jev-1.13.0", 2)
    assert out["answers"]["o"]["noul"] == 0.9
    assert s.seen[0]["auth"] == f"Bearer {KEY}"
    assert s.seen[0]["body"] == {"state": {"a": 1}, "model": "jev-1.13.0", "questions": {"o": {"type": "noul", "instructions": "?"}}}


@pytest.mark.parametrize("status", [429, 503, 529])
def test_transient_errors_retry_within_budget(api, status):
    s = api((status, {"error": "busy"}, 0), (200, OK, 0))
    assert jev.call({}, {}, "m", 3)["model"] == "jev-1.13.0"
    assert len(s.seen) == 2


def test_persistent_503_gives_up_inside_the_budget(api):
    api((503, {"error": "no healthy upstream"}, 0))
    start = time.monotonic()
    with pytest.raises(jev.JevError):
        jev.call({}, {}, "m", 1.0)
    assert time.monotonic() - start < 1.6


def test_slow_api_times_out_inside_the_budget(api):
    api((200, OK, 3))
    start = time.monotonic()
    with pytest.raises(jev.JevError):
        jev.call({}, {}, "m", 0.5)
    assert time.monotonic() - start < 1.5


def test_auth_error_does_not_retry_and_never_leaks_the_key(api):
    s = api((401, {"error": f"bad key {KEY}"}, 0))
    with pytest.raises(jev.JevError) as err:
        jev.call({}, {}, "m", 2)
    assert len(s.seen) == 1
    assert KEY not in str(err.value) and "[redacted]" in str(err.value)


def test_unreachable_api(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.setattr(jev, "API", "http://127.0.0.1:9/v1/systemone")
    with pytest.raises(jev.JevError):
        jev.call({}, {}, "m", 1)


def test_key_from_env_file_and_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"OTHER=1\nTYPESAFE_API_KEY='{KEY}'\n")
    monkeypatch.setenv("GATEKEEPER_ENV_FILE", str(env))
    assert jev.load_key() == KEY
    monkeypatch.setenv("GATEKEEPER_ENV_FILE", str(tmp_path / "missing"))
    with pytest.raises(jev.JevError) as err:
        jev.load_key()
    assert KEY not in str(err.value)


def test_key_never_reaches_log_or_state_through_a_failing_hook(api, book, monkeypatch):
    api((401, {"error": f"echo {KEY}"}, 0))
    from gatekeeper import hooks
    hooks.user_prompt(book, {"prompt": "fix my canva deck", "cwd": "/x", "session_id": "k"})
    for path in store.home().rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(), path


# ---------- CLI ----------

def test_cli_lint_roster_judge_explain_log(capsys, monkeypatch):
    monkeypatch.setattr(engine.jev, "call", FakeJev(confidence=0.95))
    assert cli.main(["lint", "agent-selection"]) == 0
    assert cli.main(["roster", "agent-selection"]) == 0
    assert cli.main(["judge", "agent-selection", "fix slide 3 on my canva deck"]) == 0
    out = capsys.readouterr().out
    assert "verdict:    route -> design-agent" in out
    assert cli.main(["judge", "agent-selection", "fix slide 3 on my canva deck", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "design-agent"
    assert cli.main(["explain", "agent-selection", "fix slide 3 on my canva deck"]) == 0
    out = capsys.readouterr().out
    assert "rules fired:  none" in out and "question target: choice over" in out
    assert cli.main(["log"]) == 0


def test_cli_unknown_gate_and_mode_roundtrip(capsys, tmp_path):
    assert cli.main(["lint", "no-such-gate"]) == 2
    copy = tmp_path / "g.yaml"
    shutil.copy(rulebook.resolve("agent-selection"), copy)
    assert cli.main(["mode", str(copy), "off"]) == 0
    assert rulebook.load(str(copy))["mode"] == "off"


def test_cli_lint_catches_broken_rulebooks(tmp_path, capsys):
    good = rulebook.resolve("agent-selection").read_text()
    cases = {
        "bad-regex": good.replace(r"'^\s*[/!]'", "'(unclosed'"),
        "bad-band": good.replace("bands: {act: 0.85, confirm: 0.60}", "bands: {act: 0.5, confirm: 0.9}"),
        "unknown-judgment": good.replace("when: {judgment: target, band: act}", "when: {judgment: nope, band: act}"),
        "bad-mode": good.replace("mode: shadow", "mode: loud"),
    }
    for name, text in cases.items():
        assert text != good, name
        path = tmp_path / f"{name}.yaml"
        path.write_text(text)
        assert cli.main(["lint", str(path)]) == 1, name


def test_hook_subprocess_always_exits_zero_with_clean_stdout(tmp_path):
    """The real entrypoint Claude Code runs: garbage in must mean exit 0 and no stdout."""
    env = {**os.environ, "GATEKEEPER_HOME": str(tmp_path / "gk"), "TYPESAFE_API_KEY": "x"}
    cmd = [sys.executable, str(REPO / "bin" / "gatekeeper"), "hook"]
    for kind, gate, stdin in [
        ("user-prompt", "agent-selection", "not json"),
        ("user-prompt", "no-such-gate", "{}"),
        ("pre-tool", "agent-selection", '{"tool_name": "Agent"}'),
        ("pre-tool", "agent-selection", ""),
        ("user-prompt", "agent-selection", '{"prompt": "ok", "session_id": "s"}'),
    ]:
        r = subprocess.run(cmd + [kind, "--gate", gate], input=stdin, capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, (kind, gate, r.stderr)
        assert r.stdout == "", (kind, gate, r.stdout)
