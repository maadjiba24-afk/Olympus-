#!/bin/bash
# Local dev setup — install Olympus with its test extra so the FULL test suite
# runs instead of skipping ~290 tests. Mirrors the Claude-Code-on-the-web
# SessionStart hook (.claude/hooks/session-start.sh) for humans running locally.
#
#   ./scripts/dev-setup.sh   # then:  pytest -q
#
# Installs cryptography (with its native cffi backend), openai, websockets, mcp,
# pyyaml, and pytest. Idempotent — safe to re-run.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing olympus-council[test] (editable)…"
python -m pip install -e '.[test]'

echo -n "Crypto backend: "
if python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; Ed25519PrivateKey.generate()" 2>/dev/null; then
  echo "OK — the full suite will run."
else
  echo "UNAVAILABLE — the 'cffi' native backend is missing; some crypto tests will skip."
  echo "  Try:  python -m pip install --force-reinstall cffi cryptography"
fi

cat <<'MSG'

Done. Run the suite with:
  pytest -q

Some tests still self-skip unless their external infra is present (opt-in):
  - real-browser tests:  OLYMPUS_BROWSER_SMOKE=1 / OLYMPUS_BROWSER_REAL=1 pytest tests/test_browser_*.py   (needs Chromium)
  - docker sandbox:      needs a running docker daemon + `docker pull python:3.11-slim`
  - live-cloud provider: needs real Bedrock/Azure creds + OLYMPUS_*_TEST_* env vars
MSG
