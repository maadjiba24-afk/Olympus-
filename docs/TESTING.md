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

## Reproducing a CI leg exactly (before you push)

`dev-setup.sh` gives you a *working* environment. It does not give you *CI's*
environment, and the gap between them is where a specific class of bug lives —
one that is invisible locally and red on the runner. Four of them shipped to CI
during the Wave-0 hardening:

| What broke | Why local didn't see it |
|---|---|
| `build` missing from `[test]` | the dev venv already had it from an earlier install |
| `setuptools` version mismatch | the release policy pins an exact version; the dev venv had another |
| `tomllib` imported unguarded | stdlib on 3.11+, absent on 3.10 — and the dev machine was 3.11 |
| a POSIX assumption in a test | Linux passed, Windows failed |

So there is a script that builds a **clean** venv on a **chosen interpreter**
and runs the CI `test` job's steps verbatim:

```bash
./scripts/ci-local.sh                # python3.10 — the MINIMUM supported
./scripts/ci-local.sh 3.12           # any leg of the matrix
./scripts/ci-local.sh 3.10 --fast    # guards + import smoke, skip pytest (~1 min)
./scripts/ci-local.sh 3.10 --reuse   # keep the venv while iterating
```

```powershell
.\scripts\ci-local.ps1                    # same, on Windows
.\scripts\ci-local.ps1 -PythonVersion 3.12
.\scripts\ci-local.ps1 -Fast -Reuse
```

**Default to 3.10.** It is the oldest version `requires-python` allows, so it is
where version-dependent breakage appears first — and it is the leg least likely
to match your daily interpreter.

The venv lives in `.venv/ci-py<version>` (gitignored) and is rebuilt from
scratch unless you pass `--reuse`. Rebuilding is the point: a reused
environment accumulates packages, which is exactly how the `build` and
`setuptools` failures stayed hidden.

## Import smoke

```bash
python scripts/import_smoke.py          # ~2 seconds
```

Imports every module in `olympus/` and `scripts/` on the current interpreter.
`compileall` proves a file *parses*; this proves it *imports*, which is a
different thing — a missing stdlib module parses perfectly. It runs in CI on
every `test` leg before pytest, so an import-time break is named in seconds
instead of surfacing as a 6-minute collection failure.

## Test timeouts

`pyproject.toml` sets `[tool.pytest.ini_options] timeout = 300`, and every CI
job carries a `timeout-minutes` ceiling. Neither is there to catch slow tests —
nothing in the suite comes close to 300s. They exist so a *hang* fails fast and
named. A deadlocking test harness once presented as "py3.11 fails at 9m where
it normally passes at 5m30s": a duration anomaly with no error message, only
diagnosable by comparing runs. With a per-test ceiling, pytest names the test.

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
