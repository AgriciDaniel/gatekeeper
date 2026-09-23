"""Local state and the audit log, under GATEKEEPER_HOME (default <repo>/.gatekeeper).

- log/YYYY-MM-DD.jsonl  one line per decision: verdict, answers, action taken.
  The prompt is stored as a sha256 plus a short excerpt, not in full.
- state/<gate>/<session>.json  the latest verdict per gate and Claude Code
  session, so the PreToolUse hook can enforce what UserPromptSubmit decided
  this turn. Keyed by gate, so two gates in one session never overwrite each other.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .redact import redact
from .rulebook import REPO

EXCERPT_CHARS = 120
STATE_TTL_S = 2 * 3600  # a turn's delegations happen soon after its prompt


def home() -> Path:
    return Path(os.environ.get("GATEKEEPER_HOME", REPO / ".gatekeeper")).expanduser()


def private_dir(path: Path) -> Path:
    """Create a folder (and its parents under home) readable by this user only."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in [path, *path.parents]:
        if d == home().parent:
            break
        try:
            d.chmod(0o700)
        except OSError:
            pass
    return path


def private_write(path: Path, text: str, append: bool = False) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC), 0o600)
    with os.fdopen(fd, "a" if append else "w") as fh:
        fh.write(text)


def fingerprint(text: str) -> dict:
    flat = " ".join(redact(text or "").split())
    return {
        "prompt_sha256": hashlib.sha256((text or "").encode()).hexdigest(),
        "prompt_excerpt": flat[:EXCERPT_CHARS] + ("…" if len(flat) > EXCERPT_CHARS else ""),
    }


def log(record: dict) -> None:
    now = datetime.now(timezone.utc)
    path = private_dir(home() / "log") / f"{now:%Y-%m-%d}.jsonl"
    private_write(path, json.dumps({"ts": now.isoformat(timespec="seconds"), **record}) + "\n", append=True)


def read_log(days: int = 7) -> list[dict]:
    rows = []
    for path in sorted((home() / "log").glob("*.jsonl"))[-days:]:
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _session_file(session_id: str, gate: str) -> Path:
    def safe(text):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", text)

    return home() / "state" / safe(gate) / f"{safe(session_id)}.json"


def clear_verdict(session_id: str | None, gate: str) -> None:
    if session_id:
        _session_file(session_id, gate).unlink(missing_ok=True)


def save_verdict(session_id: str | None, verdict: dict) -> None:
    """Atomic write (tmp + rename), so a killed hook never leaves half a file."""
    if not session_id:
        return
    path = _session_file(session_id, verdict["gate"])
    private_dir(path.parent)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    private_write(tmp, json.dumps({"at": time.time(), "verdict": verdict}))
    os.replace(tmp, path)
    _prune(path.parent)


def _prune(gate_dir: Path) -> None:
    """Drop verdicts past their TTL, and v0.1 files kept directly under state/."""
    cutoff = time.time() - STATE_TTL_S
    for old in list(gate_dir.glob("*.json")) + list(gate_dir.parent.glob("*.json")):
        try:
            if old.stat().st_mtime < cutoff or old.parent == gate_dir.parent:
                old.unlink(missing_ok=True)
        except OSError:
            pass


def load_verdict(session_id: str | None, gate: str) -> dict | None:
    if not session_id:
        return None
    try:
        data = json.loads(_session_file(session_id, gate).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - data.get("at", 0) > STATE_TTL_S:
        return None
    return data.get("verdict")
