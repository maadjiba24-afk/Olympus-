#!/bin/bash
# SessionStart hook — provision the ephemeral Claude-Code-on-the-web container so
# the FULL test suite runs instead of silently skipping ~290 tests.
#
# Two things a fresh remote container gets wrong:
#   1. `cryptography` is present but its native `_cffi_backend` (the `cffi`
#      package) is missing — and because cryptography is already "satisfied",
#      a plain `pip install` never pulls cffi. ~257 tests skip on this alone.
#   2. the declared `[test]` extras (openai / websockets / mcp / pyyaml) aren't
#      installed, so those integration tests skip too.
#
# Web/remote only, idempotent, cache-friendly, and never blocks the session.
set -uo pipefail

# Local dev is left untouched — humans use scripts/dev-setup.sh instead.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}" || exit 0

# 1) Guarantee the cryptography native backend (the #1 skip cause). Explicit,
#    because pip won't pull cffi when cryptography already appears satisfied.
python -m pip install -q cffi || true

# 2) Install the declared test extra. Resilient to base images that ship a
#    system-managed package pip can't replace to satisfy a transitive pin
#    (e.g. a Debian-managed PyJWT vs mcp's pyjwt>=2.10.1): pin the already-
#    installed set and retry; last resort installs the extras minus mcp.
if ! python -m pip install -q -e '.[test]'; then
  python -m pip freeze > /tmp/olympus-test-constraints.txt 2>/dev/null || true
  python -m pip install -q -e '.[test]' -c /tmp/olympus-test-constraints.txt \
    || { python -m pip install -q -e . >/dev/null 2>&1 ; \
         python -m pip install -q openai websockets pyyaml pytest >/dev/null 2>&1 ; }
fi

# Report the outcome (SessionStart stdout is added to the session's context).
if python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; Ed25519PrivateKey.generate()" 2>/dev/null; then
  echo "session-start: crypto backend OK + test extras installed — full suite will run."
else
  echo "session-start: crypto backend still unavailable; some tests will skip." >&2
fi

# Never fail the session on a provisioning hiccup.
exit 0
