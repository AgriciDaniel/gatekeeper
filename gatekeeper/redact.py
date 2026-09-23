"""Mask secret-looking strings before a prompt leaves the machine or hits the log.

Best effort, not a guarantee: it catches the loaded TypeSafe key exactly and
common token shapes (API keys, bearer tokens, JWTs, private key blocks,
credentials in URLs, key=value secrets), not every secret.
"""
from __future__ import annotations

import re

MASK = "[redacted]"

PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(sk|pk|rk)[-_](live_|test_)?[A-Za-z0-9_-]{16,}"),      # OpenAI, Anthropic, Stripe
    re.compile(r"\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),       # GitHub
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),                            # GitLab
    re.compile(r"\bxox[abprse]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),                           # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                              # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),                            # Google OAuth token
    re.compile(r"\b(hf|npm)_[A-Za-z0-9]{20,}"),                            # Hugging Face, npm
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),            # SendGrid
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?<=://)[^\s:/@]+:[^\s@/]+(?=@)"),                        # user:pass@ in URLs
    re.compile(r"(?i)\bcurl\b[^\n]*?\s-u\s+\S+:\S+"),
    re.compile(r"(?i)[\"']?\b[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|AUTH)[\"']?\s*[=:]\s*"
               r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,}]{6,})"),
]


def _known_secrets() -> list[str]:
    """The TypeSafe key this process would use, masked exactly wherever it appears."""
    try:
        from .jev import load_key

        key = load_key()
    except Exception:
        return []
    return [key] if len(key) >= 8 else []


def redact(text: str) -> str:
    if not text:
        return text
    for secret in _known_secrets():
        text = text.replace(secret, MASK)
    for pattern in PATTERNS:
        text = pattern.sub(MASK, text)
    return text
