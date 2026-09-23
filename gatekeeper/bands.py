"""Confidence bands: turn a Jev answer into act / confirm / escalate.

Choice and Score answers carry `confidence`; a Noul answer does not, so a
Noul is banded on its probability of yes with a review band in the middle
(docs.typesafe.ai/primitives/noul, docs.typesafe.ai/confidence).
Every threshold here comes from the rulebook, never from this file.
"""
from __future__ import annotations


def confidence_band(confidence: float, bands: dict) -> str:
    if confidence >= bands["act"]:
        return "act"
    if confidence >= bands["confirm"]:
        return "confirm"
    return "escalate"


def noul_band(p: float, bands: dict) -> str:
    if p >= bands["yes"]:
        return "yes"
    if p <= bands["no"]:
        return "no"
    return "uncertain"
