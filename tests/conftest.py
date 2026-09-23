import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fake ~/.claude with two agents, three skills, and one enabled plugin."""
    claude = tmp_path / "claude"
    _write(claude / "agents" / "design-agent.md", "---\nname: design-agent\ndescription: Canva designs\n---\nbody")
    _write(claude / "agents" / "code-agent.md", "---\nname: code-agent\ndescription: >\n  Rust code\n  and crates\n---\n")
    _write(claude / "skills" / "blog" / "SKILL.md", "---\nname: blog\ndescription: Blog hub\n---\n")
    _write(claude / "skills" / "blog-write" / "SKILL.md", "---\nname: blog-write\ndescription: Write a post\n---\n")
    _write(claude / "skills" / "canva-edit-design" / "SKILL.md", "---\nname: canva-edit-design\ndescription: Edit\n---\n")
    plugin = tmp_path / "plugins" / "demo"
    _write(plugin / "skills" / "blog-write" / "SKILL.md", "---\nname: blog-write\ndescription: duplicate\n---\n")
    _write(plugin / "agents" / "helper.md", "---\nname: helper\ndescription: Demo helper\n---\n")
    _write(claude / "plugins" / "installed_plugins.json",
           json.dumps({"version": 2, "plugins": {"demo@m": [{"installPath": str(plugin)}], "off@m": [{"installPath": "/nope"}]}}))
    _write(claude / "settings.json", json.dumps({"enabledPlugins": {"demo@m": True, "off@m": False}}))
    monkeypatch.setenv("GATEKEEPER_CLAUDE_DIR", str(claude))
    monkeypatch.setenv("GATEKEEPER_HOME", str(tmp_path / "gk"))
    monkeypatch.delenv("GATEKEEPER", raising=False)
    # never read the developer's private rulebooks or repo .env key file
    monkeypatch.setenv("GATEKEEPER_LOCAL", str(tmp_path / "local-gates"))
    monkeypatch.delenv("GATEKEEPER_ENV_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


BOOK = {
    "gate": "demo",
    "version": 3,
    "model": "jev-1.13.0",
    "mode": "enforce",
    "fail": "open",
    "state": {"fields": ["prompt", "project"]},
    "roster": {"exclude": ["blog-*", "canva-*"], "keep": ["design-agent"]},
    "families": {"canva": ["design-agent", "canva-*"], "blog": ["blog", "blog-*"]},
    "neutral": ["Explore", "general-purpose"],
    "hard_rules": [
        {"id": "cmd", "when": {"field": "prompt", "matches": r"^\s*/"}, "verdict": "allow"},
        {"id": "canva", "when": {"field": "prompt", "matches": r"(?i)\bcanva\b"}, "candidates": ["design-agent"]},
        {"id": "no-rust", "when": {"field": "prompt", "matches": r"(?i)\bno rust\b"}, "forbid": ["code-agent"]},
    ],
    "judgments": {
        "t": {"type": "choice", "instructions": "Who handles `prompt`?", "options": "roster",
              "escape": {"main_thread": "No specialist"}, "bands": {"act": 0.85, "confirm": 0.6}},
        "o": {"type": "noul", "instructions": "Outside action?", "bands": {"yes": 0.7, "no": 0.3}},
    },
    "decide": [
        {"id": "main", "when": {"judgment": "t", "choice_is_escape": True}, "verdict": "allow"},
        {"id": "outside", "when": {"judgment": "o", "band": "yes"}, "verdict": "confirm", "target": "{t.choice}",
         "message": "confirm first (p={o.noul})"},
        {"id": "route", "when": {"judgment": "t", "band": "act"}, "verdict": "route", "target": "{t.choice}",
         "message": "route to {t.choice} ({t.confidence})"},
        {"id": "suggest", "when": {"judgment": "t", "band": "confirm"}, "verdict": "suggest", "target": "{t.choice}",
         "message": "maybe {t.choice}"},
        {"id": "escalate", "verdict": "escalate", "message": "ask ({t.alternatives})"},
    ],
    "bench": {"judgment": "t", "target_precision": 0.9, "min_rows": 2},
}


@pytest.fixture
def book():
    return json.loads(json.dumps(BOOK))


class FakeJev:
    """Replays a Jev response and records what was asked."""

    def __init__(self, choice="design-agent", confidence=1.0, noul=0.03, error=None):
        self.choice, self.confidence, self.noul, self.error = choice, confidence, noul, error
        self.calls = []

    def __call__(self, state, questions, model, timeout_s):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error:
            raise self.error
        base = json.loads((FIXTURES / "jev_response_design.json").read_text())
        choice, noul = base["answers"]["t"], base["answers"]["o"]
        choice.update(choice=self.choice, confidence=self.confidence,
                      probabilities={self.choice: self.confidence, "main_thread": round(1 - self.confidence, 4)})
        noul["noul"] = self.noul
        # answer every question id the gate asked, by type, like the real API
        base["answers"] = {qid: dict(choice if q["type"] == "choice" else noul) for qid, q in questions.items()}
        return base
