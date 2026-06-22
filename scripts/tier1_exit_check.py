#!/usr/bin/env python3
"""Tier-1 exit gate CLI — runs the replay gate over real multi-step prompts.

The plan gates Tier 2 on: `olympus ask "<real multi-step task>"` completes
unattended, producing a replayable log, on 3 consecutive distinct prompts. This
runs exactly that (see olympus.replaygate for the logic). The record pass makes
real model calls, so a provider key must be configured; the replay pass makes
none. Run it where the key lives — e.g. the deployed instance:

    python scripts/tier1_exit_check.py
    python scripts/tier1_exit_check.py "prompt one" "prompt two" "prompt three"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from olympus import firstrun, replaygate  # noqa: E402


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    firstrun.load_env_file()
    all_pass, results = replaygate.run_exit_check(argv or None)
    passed = sum(1 for r in results if replaygate._ok(r))
    print(f"\n{'=' * 60}")
    print(f"Tier-1 exit gate: {passed}/{len(results)} prompts passed.")
    if all_pass:
        print("✓ GATE MET — runs complete unattended and replay byte-identically. "
              "Cleared for Tier 2.")
        return 0
    print("✗ GATE NOT MET — see the failing prompt(s) above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
