"""Deterministic predicates: the part of a gate that code decides.

A predicate is a small dict evaluated against an event (a flat dict of
fields). Leaf forms name a `field` and one operator; `all`, `any`, and `not`
combine them. Anything a string match or comparison can decide belongs here,
not in a Jev question.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

OPERATORS = ("matches", "equals", "in", "shorter_than", "longer_than", "exists")


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def match(pred: dict, event: dict) -> bool:
    if "all" in pred:
        return all(match(p, event) for p in pred["all"])
    if "any" in pred:
        return any(match(p, event) for p in pred["any"])
    if "not" in pred:
        return not match(pred["not"], event)

    value: Any = event.get(pred["field"])
    if "exists" in pred:
        return (value not in (None, "")) == bool(pred["exists"])
    if value is None:
        return False
    if "matches" in pred:
        return _compile(pred["matches"]).search(str(value)) is not None
    if "equals" in pred:
        return value == pred["equals"]
    if "in" in pred:
        return value in pred["in"]
    if "shorter_than" in pred:
        return len(str(value).strip()) < pred["shorter_than"]
    if "longer_than" in pred:
        return len(str(value).strip()) > pred["longer_than"]
    raise ValueError(f"predicate has no known operator: {pred}")
