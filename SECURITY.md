# Security

## Reporting a problem

Please report security issues privately through GitHub's **Report a vulnerability** button on the Security tab of this repository, not in a public issue. Include the Gatekeeper version, what you ran, what happened and what you expected. You will get a reply within a week.

## What Gatekeeper sends and stores

- Each judged prompt (up to 6000 characters, with your TypeSafe key and common secret shapes masked first) is sent to the TypeSafe API under your own key, in every mode except `off`.
- The local log in `.gatekeeper/` keeps a SHA-256 and a 120-character masked excerpt per prompt, never the full prompt. Log, state and bench files are readable only by your user.
- The key is read from `TYPESAFE_API_KEY`, `GATEKEEPER_ENV_FILE` or `~/.config/gatekeeper/env`, never from the repository, and is never printed or logged.

## Design rules that are security properties

- Hooks fail open: any failure, including a missing dependency or a bad argument, lets the prompt through and never blocks a session.
- Gatekeeper never takes an outside action (send, publish, deploy, spend). It only decides who handles work.
- Handler names and paths from an index file are sanitized before they reach the assistant's context.

Masking is best effort. Do not install Gatekeeper where prompts may not leave your machine.
