"""Load and validate a gate's YAML rulebook.

A gate name resolves in this order: gates/local/<gate>.yaml (your private
override, git-ignored), gates/<gate>.yaml, then examples/<gate>/<gate>.yaml.
GATEKEEPER_LOCAL points the private override folder somewhere else.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
GATES = REPO / "gates"
SCHEMA = GATES / "_schema.json"
EXAMPLES = REPO / "examples"
MODES = ("shadow", "advise", "enforce", "off")


def local_dir() -> Path:
    return Path(os.environ.get("GATEKEEPER_LOCAL", GATES / "local")).expanduser()


class RulebookError(ValueError):
    pass


def resolve(gate: str) -> Path:
    """A gate name (`agent-selection`) or a path to a YAML file."""
    path = Path(gate)
    if path.suffix in (".yaml", ".yml") and path.exists():
        return path.resolve()
    for candidate in (local_dir() / f"{gate}.yaml", GATES / f"{gate}.yaml", EXAMPLES / gate / f"{gate}.yaml"):
        if candidate.exists():
            return candidate
    raise RulebookError(f"no gate named {gate!r} in {local_dir()}, {GATES} or {EXAMPLES}")


def identity(gate: str) -> str:
    """One key per rulebook file, so `marketing-team` and its path compare equal."""
    try:
        return str(resolve(gate).resolve())
    except RulebookError:
        return gate


def relative_to_book(book: dict, file: str) -> Path:
    """A path in a rulebook: absolute, ~, or relative to the rulebook's folder
    (falling back to the repo root, where older rulebooks kept their bench)."""
    path = Path(file).expanduser()
    if path.is_absolute():
        return path
    here = Path(book.get("_path", REPO / "x")).parent / path
    return here if here.exists() or not (REPO / path).exists() else REPO / path


def load(gate: str, validate: bool = True) -> dict:
    path = resolve(gate)
    try:
        book = yaml.safe_load(path.read_text())
    except yaml.YAMLError as err:
        raise RulebookError(f"{path.name}: invalid YAML: {err}") from None
    _coerce_yaml_booleans(book)
    if isinstance(book, dict) and _shipped(path):
        book["mode"] = _mode_overrides().get(str(path.resolve()), book.get("mode"))
    if validate:
        errors = schema_errors(book)
        if errors:
            raise RulebookError(f"{path.name}: " + "; ".join(errors[:5]))
    book["_path"] = str(path)
    index = (book.get("roster") or {}).get("index")
    if isinstance(index, dict) and index.get("file"):
        index["file"] = str(relative_to_book(book, index["file"]))
    return book


def _coerce_yaml_booleans(book) -> None:
    """YAML 1.1 reads unquoted yes/no/on/off/true/false as booleans. Mode,
    band keys, and criteria keys are always strings, so map them back."""
    names = {True: "yes", False: "no"}
    if isinstance(book, dict) and book.get("mode") is False:  # unquoted `mode: off`
        book["mode"] = "off"
    for entry in book.get("decide") or [] if isinstance(book, dict) else []:
        _coerce_condition(entry.get("when"), names)
    for spec in (book.get("judgments") or {}).values() if isinstance(book, dict) else []:
        if isinstance(spec.get("bands"), dict):
            spec["bands"] = {names.get(k, k) if isinstance(k, bool) else k: v for k, v in spec["bands"].items()}
        if spec.get("type") == "noul" and isinstance(spec.get("criteria"), dict):
            spec["criteria"] = {str(k).lower() if isinstance(k, bool) else k: v for k, v in spec["criteria"].items()}


def _coerce_condition(cond, names) -> None:
    """`band: yes` unquoted in a decide condition -> "yes"."""
    if not isinstance(cond, dict):
        return
    if isinstance(cond.get("band"), bool):
        cond["band"] = names[cond["band"]]
    if isinstance(cond.get("band_in"), list):
        cond["band_in"] = [names[b] if isinstance(b, bool) else b for b in cond["band_in"]]
    for key in ("all", "any"):
        for sub in cond.get(key) or []:
            _coerce_condition(sub, names)
    _coerce_condition(cond.get("not"), names)


def schema_errors(book) -> list[str]:
    import jsonschema  # imported lazily: only lint and first load need it

    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(book)]
    for rule in book.get("hard_rules", []) if isinstance(book, dict) else []:
        for pattern in _patterns(rule.get("when", {})):
            try:
                re.compile(pattern)
            except re.error as err:
                errors.append(f"hard_rules/{rule.get('id')}: bad regex {pattern!r}: {err}")
    return errors


def _patterns(pred: dict):
    if not isinstance(pred, dict):
        return
    if "matches" in pred:
        yield pred["matches"]
    for key in ("all", "any"):
        for sub in pred.get(key, []):
            yield from _patterns(sub)
    if "not" in pred:
        yield from _patterns(pred["not"])


def _modes_file() -> Path:
    return local_dir() / "modes.json"


def _mode_overrides() -> dict:
    try:
        data = json.loads(_modes_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _shipped(path: Path) -> bool:
    """A rulebook tracked in this repo (gates/ or examples/), not a private or outside file."""
    path = path.resolve()
    return any(path.is_relative_to(d.resolve()) for d in (GATES, EXAMPLES)) and not path.is_relative_to(local_dir().resolve())


def set_mode(gate: str, mode: str) -> Path:
    """Change a gate's mode. A shipped rulebook (gates/, examples/) is never
    edited: the mode goes to gates/local/modes.json, so the checkout stays clean.
    Your own rulebook gets only its top-level `mode:` line rewritten, so comments survive."""
    if mode not in MODES:
        raise RulebookError(f"unknown mode {mode!r}")
    path = resolve(gate)
    if _shipped(path):
        overrides = _mode_overrides()
        overrides[str(path.resolve())] = mode
        target = _modes_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(overrides, indent=2) + "\n")
        return target
    text, n = re.subn(r"(?m)^mode:[ \t]*\"?\w+\"?", f'mode: "{mode}"', path.read_text(), count=1)
    if n != 1:
        raise RulebookError(f"{path.name} has no top-level mode line")
    path.write_text(text)
    return path
