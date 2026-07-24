# Running the tests

Olympus's suite is designed so a bare `pytest -q` **passes in any environment** —
tests that need an optional backend (the `cryptography` native backend, the
`openai` / `mcp` / `websockets` extras) or external infrastructure (a real
browser, a Docker daemon, live cloud credentials) **self-skip** rather than fail.
That keeps the suite green on a minimal box, but it means a fresh checkout can
silently *skip a few hundred tests* until the optional pieces are installed.

## Full local run

Install the package with its `test` extra, then run the suite:

```bash
./scripts/dev-setup.sh      # pip install -e '.[test]'  + a crypto-backend check
pytest -q
```

`.[test]` pulls `cryptography` (with its native `cffi` backend), `openai`,
`websockets`, `mcp`, `pyyaml`, and `pytest` — which is what un-skips the bulk of
the suite. If crypto tests still skip, the `cffi` backend is missing; reinstall
it:

```bash
python -m pip install --force-reinstall cffi cryptography
```

## Claude Code on the web

No setup needed — the committed `SessionStart` hook
(`.claude/hooks/session-start.sh`) runs `pip install -e '.[test]'` automatically
when a web session starts, so the full suite runs out of the box. (The hook is
web/remote only; local dev uses `scripts/dev-setup.sh` above.)

## Opt-in / infrastructure-gated tests

A handful stay skipped unless you provide their infrastructure — deliberately, so
`pytest -q` never *requires* a GPU, Docker, or a cloud account:

| Tests | How to run them |
|---|---|
| Real-browser (`test_browser_smoke.py`, `test_browser_real.py`) | `OLYMPUS_BROWSER_SMOKE=1` / `OLYMPUS_BROWSER_REAL=1` + a Chromium binary on `PATH` or under `PLAYWRIGHT_BROWSERS_PATH` |
| Docker sandbox (`test_sandbox_docker_integration.py`) | a running Docker daemon + `docker pull python:3.11-slim` |
| Live cloud providers (Bedrock / Azure) | real credentials + `OLYMPUS_BEDROCK_TEST_MODEL` / `OLYMPUS_AZURE_TEST_*` |

CI exercises all of these across its job matrix (a browser job installs
Playwright; a docker job pulls the image), so nothing is left uncovered — the
skips are only about keeping the *default* run portable.
