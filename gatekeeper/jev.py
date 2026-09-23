"""Minimal TypeSafe System One client.

POST https://api.typesafe.ai/v1/systemone with `state`, `model`, and all of a
gate's `questions` in one request (questions over the same state run in
parallel). The key comes from TYPESAFE_API_KEY in the environment, then from
the env file named by GATEKEEPER_ENV_FILE, then from ~/.config/gatekeeper/env
(a symlink to an existing env file works). It is never printed, logged, or put
in an error message.

429, 503, and 529 are retried with backoff, but only inside the caller's time
budget: a gate sits in front of interactive work and must answer fast or
fail per its rulebook.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
USD_PER_MTOK_INPUT = 0.042  # docs.typesafe.ai/models, retrieved 2026-09-20; output is free


class JevError(RuntimeError):
    pass


def load_key() -> str:
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not key:
        for env_file in env_files():
            key = (_read_key(env_file) or "").strip()
            if key:
                break
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set and was not found in an env file")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        # a control character would end up in an HTTP header error that echoes the key
        raise JevError("TYPESAFE_API_KEY contains whitespace or control characters")
    return key


def env_files() -> list[Path]:
    """GATEKEEPER_ENV_FILE, then ~/.config/gatekeeper/env. Not the repo: the skill
    link exposes the repo folder under ~/.claude/skills, so a key file there travels
    with anything that copies skills."""
    files = [Path(os.environ["GATEKEEPER_ENV_FILE"]).expanduser()] if os.environ.get("GATEKEEPER_ENV_FILE") else []
    config = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    return files + [config / "gatekeeper" / "env"]


def _read_key(env_file: Path) -> str | None:
    try:
        for line in env_file.read_text().splitlines():
            name, _, value = line.strip().removeprefix("export ").partition("=")
            value = value.strip()
            if value[:1] in "'\"" and value[:1] and value[:1] in value[1:]:
                value = value[1 : value.index(value[0], 1)]
            else:
                value = value.split(" #", 1)[0].strip()
            if name.strip() == "TYPESAFE_API_KEY" and value:
                return value
    except OSError:
        pass
    return None


def call(state: dict, questions: dict, model: str, timeout_s: float) -> dict:
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    key = load_key()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JevError(f"no answer within {timeout_s}s")
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            wait = float(err.headers.get("retry-after") or 0.25 * 2**attempt)
            if err.code in (429, 503, 529) and time.monotonic() + wait < deadline:
                time.sleep(wait)
                attempt += 1
                continue
            detail = err.read().decode(errors="replace").replace(key, "[redacted]")[:200]
            raise JevError(f"TypeSafe API error {err.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise JevError(f"TypeSafe API unreachable: {str(err).replace(key, '[redacted]')}") from None
        except (ValueError, json.JSONDecodeError, http.client.HTTPException) as err:
            # never the message: a header error can quote the Authorization value
            raise JevError(f"TypeSafe request failed: {type(err).__name__}") from None


def cost_usd(input_tokens: int) -> float:
    return round(input_tokens / 1e6 * USD_PER_MTOK_INPUT, 8)
