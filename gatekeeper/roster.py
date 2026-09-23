"""Build the list of agents and skills a gate can route to.

Reads what Claude Code itself would offer: user agents (~/.claude/agents),
user skills (~/.claude/skills), and the agents and skills of every enabled
plugin (named `plugin:name`, as Claude Code names them). Built-in agents that
have no file on disk come from the rulebook's `roster.builtin`.

The `index` source reads handlers from a JSON file instead, for hosts that
route to Markdown procedures rather than Claude Code agents. Each entry keeps
its `group` (for rollup) and `path` (the file the host should read).

GATEKEEPER_CLAUDE_DIR overrides ~/.claude, for tests.
"""
from __future__ import annotations

import json
import os
import re
from fnmatch import fnmatch
from pathlib import Path

import yaml


def claude_dir() -> Path:
    return Path(os.environ.get("GATEKEEPER_CLAUDE_DIR", "~/.claude")).expanduser()


def base(name: str) -> str:
    """`blog-plugin:blog-write` -> `blog-write`."""
    return name.rsplit(":", 1)[-1]


def matches_any(name: str, patterns) -> bool:
    return any(fnmatch(name, p) or fnmatch(base(name), p) for p in patterns)


def matches_any_name(pattern: str, names) -> bool:
    """Does glob `pattern` match at least one of `names` (full or base name)?"""
    return any(fnmatch(n, pattern) or fnmatch(base(n), pattern) for n in names)


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _entry(path: Path, kind: str, fallback_name: str, prefix: str = "") -> tuple[str, dict] | None:
    meta = _frontmatter(path)
    name = str(meta.get("name") or fallback_name)
    desc = " ".join(str(meta.get("description") or "").split())
    return f"{prefix}{name}", {"kind": kind, "description": desc, "source": str(path)}


def _enabled_plugins(root: Path) -> list[tuple[str, Path]]:
    try:
        installed = json.loads((root / "plugins" / "installed_plugins.json").read_text())["plugins"]
        enabled = json.loads((root / "settings.json").read_text()).get("enabledPlugins", {})
    except (OSError, KeyError, json.JSONDecodeError):
        return []
    out = []
    for key, value in installed.items():
        if not enabled.get(key):
            continue
        record = value[0] if isinstance(value, list) else value
        out.append((key.split("@")[0], Path(record["installPath"])))
    return out


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _safe_path(value) -> str | None:
    """An index path is shown to the assistant as "read this file": keep only a
    relative path inside the index's own tree, with no control or quoting characters."""
    text = str(value or "")
    if not text or len(text) > 300 or any(ord(ch) < 32 for ch in text) or any(ch in text for ch in "`$<>|;&"):
        return None
    path = Path(text)
    if path.is_absolute() or text.startswith("~") or ".." in path.parts:
        return None
    return text


def _singular(key: str) -> str:
    """`skills` -> `skill`, `entries` -> `entry`, `workflows` -> `workflow`."""
    if key.endswith("ies"):
        return key[:-3] + "y"
    return key[:-1] if key.endswith("s") and len(key) > 1 else key


def _index_entries(cfg: dict) -> list[tuple[str, dict]]:
    index = cfg.get("index") or {}
    path = Path(index.get("file", "")).expanduser()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
        return []
    if isinstance(data, list):
        groups = [("entry", data)]
    elif isinstance(data, dict):
        groups = [(_singular(key), data.get(key) or []) for key in index.get("collections") or list(data)]
    else:
        return []
    name_field = index.get("name", "id")
    desc_fields = index.get("description", ["description"])
    if isinstance(desc_fields, str):
        desc_fields = [desc_fields]
    where = index.get("where") or {}
    out = []
    for kind, items in groups:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get(name_field):
                continue
            if any(item.get(k) != v for k, v in where.items()):
                continue
            name = str(item[name_field])
            if not SAFE_NAME.fullmatch(name):
                continue  # names are echoed into the assistant's context: plain identifiers only
            parts = [" ".join(str(item.get(f) or "").split()) for f in desc_fields]
            entry = {"kind": kind, "description": " ".join(p for p in parts if p), "source": str(path)}
            if index.get("group"):
                group = str(item.get(index["group"]) or "")
                entry["group"] = group if SAFE_NAME.fullmatch(group) else None
            if index.get("path"):
                entry["path"] = _safe_path(item.get(index["path"]))
            out.append((name, entry))
    return out


def build(cfg: dict) -> dict[str, dict]:
    """Return {name: {kind, description, source}} after exclude and dedupe.

    A plugin entry whose base name already exists at user level is dropped,
    so a skill installed both ways is offered once.
    """
    root = claude_dir()
    sources = set(cfg.get("sources", ["user_agents", "user_skills", "plugin_agents", "plugin_skills"]))
    found: list[tuple[str, dict]] = []

    for name, desc in (cfg.get("builtin") or {}).items():
        found.append((name, {"kind": "agent", "description": " ".join(desc.split()), "source": "builtin"}))
    if "user_agents" in sources:
        for p in sorted((root / "agents").glob("*.md")):
            found.append(_entry(p, "agent", p.stem))
    if "user_skills" in sources:
        for p in sorted((root / "skills").glob("*/SKILL.md")):
            found.append(_entry(p, "skill", p.parent.name))
    if "index" in sources:
        found += _index_entries(cfg)
    for plugin, path in _enabled_plugins(root) if sources & {"plugin_agents", "plugin_skills"} else []:
        if "plugin_agents" in sources:
            for p in sorted((path / "agents").glob("*.md")):
                found.append(_entry(p, "agent", p.stem, f"{plugin}:"))
        if "plugin_skills" in sources:
            for p in sorted((path / "skills").glob("*/SKILL.md")):
                found.append(_entry(p, "skill", p.parent.name, f"{plugin}:"))

    exclude = cfg.get("exclude", [])
    keep = cfg.get("keep", [])
    limit = cfg.get("description_chars", 280)
    roster: dict[str, dict] = {}
    for name, entry in found:
        if name in roster or base(name) in roster:
            continue
        if matches_any(name, exclude) and not matches_any(name, keep):
            continue
        desc = entry["description"]
        entry["description"] = desc if len(desc) <= limit else desc[: limit - 1].rsplit(" ", 1)[0] + "…"
        roster[name] = entry
    return roster
