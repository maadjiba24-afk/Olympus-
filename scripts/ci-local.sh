#!/usr/bin/env bash
# Reproduce one leg of the CI `test` job EXACTLY, locally, before pushing.
#
# `scripts/dev-setup.sh` gives you a working environment. This gives you CI's
# environment, and the difference is where the bugs live. Four failures in the
# Wave-0/#245 work were invisible locally and red on CI, every one of them
# because the local venv differed from the runner's:
#
#   * `build` was absent from the `test` extra — the local venv happened to
#     have it from an earlier install.
#   * `setuptools` was a different version than the release policy pins, so a
#     wheel-metadata assertion failed only on the runner.
#   * `tomllib` was imported unguarded — invisible on 3.11+, fatal on the 3.10
#     leg, and the dev machine was 3.11.
#   * a test's POSIX assumption passed on Linux and failed on Windows.
#
# What CI does, and therefore what this does, in order:
#     pip install --require-hashes -r requirements.lock
#     pip install -e . --no-deps
#     pip install -e '.[test]'
#     python scripts/check_no_prerelease.py requirements.lock
#     python -m compileall -q olympus
#     python -m olympus capabilities --check
#     python scripts/check_threat_model.py
#     pytest -q
#
# Usage:
#     ./scripts/ci-local.sh                # 3.10 — the MINIMUM supported, and
#                                          #   where version breakage surfaces
#     ./scripts/ci-local.sh 3.12           # any leg of the matrix
#     ./scripts/ci-local.sh 3.10 --reuse   # keep the venv (fast iteration)
#     ./scripts/ci-local.sh 3.10 --fast    # guards + import smoke, skip pytest
#
# The venv lives at .venv/ci-py<version> (already gitignored). A fresh one is
# built by default: a reused environment accumulates packages and is exactly
# how the `build` and `setuptools` failures stayed hidden.
set -uo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

# The minimum supported version, and this script's default. Kept in a variable
# so the "not found" help can tell whether the caller asked for the minimum leg
# or deliberately chose another one.
DEFAULT_PYVER="3.10"
PYVER="$DEFAULT_PYVER"
REUSE=0
FAST=0
for arg in "$@"; do
  case "$arg" in
    --reuse) REUSE=1 ;;
    --fast)  FAST=1 ;;
    3.*)     PYVER="$arg" ;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

VENV=".venv/ci-py${PYVER}"

# Interpreter discovery. A versioned binary on PATH is also where
# `uv python install` drops its shim (~/.local/bin/python3.10); fall back to
# asking uv directly, which covers a uv-managed interpreter whose shim dir is
# not on PATH. Kept in step with the same two tiers in ci-local.ps1.
PYBIN="python${PYVER}"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
  PYBIN=""
  if command -v uv >/dev/null 2>&1; then
    FOUND="$(uv python find "$PYVER" 2>/dev/null || true)"
    [ -n "$FOUND" ] && [ -x "$FOUND" ] && PYBIN="$FOUND"
  fi
fi
if [ -z "$PYBIN" ]; then
  echo "Python ${PYVER} not found. Tried 'python${PYVER}' on PATH and 'uv python find ${PYVER}'." >&2
  echo >&2
  echo "Install it with any of:" >&2
  echo "    uv python install ${PYVER}     # recommended; puts python${PYVER} on PATH" >&2
  echo "    pyenv install ${PYVER}" >&2
  echo "    sudo apt install python${PYVER} python${PYVER}-venv    # Debian/Ubuntu + deadsnakes" >&2
  echo >&2
  if [ "$PYVER" = "$DEFAULT_PYVER" ]; then
    echo "${PYVER} is the MINIMUM supported version and the leg where version-dependent" >&2
    echo "breakage surfaces first, so prefer installing it over switching. If you must:" >&2
    echo "    ./scripts/ci-local.sh 3.12" >&2
  else
    echo "Or run the default minimum-version leg:  ./scripts/ci-local.sh" >&2
  fi
  exit 2
fi

FAILED=()
step() {
  local label="$1"; shift
  printf '\n\033[1m▶ %s\033[0m\n' "$label"
  if "$@"; then
    printf '\033[32m✓ %s\033[0m\n' "$label"
  else
    printf '\033[31m✗ %s\033[0m\n' "$label"
    FAILED+=("$label")
    return 1
  fi
}

if [ "$REUSE" -eq 0 ] || [ ! -d "$VENV" ]; then
  echo "building a clean venv at $VENV (python${PYVER})…"
  rm -rf "$VENV"
  "$PYBIN" -m venv "$VENV" || exit 2
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip >/dev/null 2>&1

echo "interpreter: $(python --version 2>&1)"

# --- the CI install sequence, verbatim -------------------------------------
step "install hash-pinned lock"   pip install -q --require-hashes -r requirements.lock || true
step "install package (no deps)"  pip install -q -e . --no-deps || true
step "install [test] extra"       pip install -q -e '.[test]' || true

# --- the CI guard steps, in CI's order -------------------------------------
step "no pre-release deps"        python scripts/check_no_prerelease.py requirements.lock || true
step "compileall"                 python -m compileall -q olympus || true
# Mirrors the ci.yml step of the same name; runs before the suite because it
# is ~2s and names import-time breakage the 6-minute suite finds much later.
step "import smoke"               python scripts/import_smoke.py --quiet || true
step "capability counts"          python -m olympus capabilities --check || true
step "threat model"               python scripts/check_threat_model.py || true

if [ "$FAST" -eq 0 ]; then
  step "pytest" pytest -q || true
else
  echo "(--fast: pytest skipped)"
fi

printf '\n────────────────────────────────────────\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\033[32mall steps passed on python%s\033[0m\n' "$PYVER"
  exit 0
fi
printf '\033[31m%d step(s) failed on python%s:\033[0m\n' "${#FAILED[@]}" "$PYVER"
for f in "${FAILED[@]}"; do printf '  - %s\n' "$f"; done
exit 1
