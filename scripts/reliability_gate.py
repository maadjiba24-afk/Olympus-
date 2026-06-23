#!/usr/bin/env python3
"""Operator-run reliability gate — the operational release-readiness check.

The CI replay gate proves *recorded* runs replay; this proves the harness works
**unattended end-to-end on a real key**: it runs 3 distinct prompts through the
full pipeline, and for each confirms a decision log was produced and that the
run replays **zero-API and byte-identically** (`orchestrator.replay_run` →
empty diff). It enforces a spend cap and reports total spend.

Reuses `olympus.replaygate` (same record→replay→byte-identical check the
heartbeat/CI gates use). Needs ANTHROPIC_API_KEY; run it where the key lives:

    python scripts/reliability_gate.py
    OLYMPUS_DAILY_BUDGET=10 python scripts/reliability_gate.py "p1" "p2" "p3"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_CAP = "5.00"   # USD; mirrors replay-gate.yml's cap


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # Enforce a spend cap (respect one already set by the operator).
    os.environ.setdefault("OLYMPUS_DAILY_BUDGET", _DEFAULT_CAP)

    from olympus import firstrun, replaygate, usage
    firstrun.load_env_file()

    cap = float(os.environ.get("OLYMPUS_DAILY_BUDGET") or 0)
    before = usage.today_spend()
    all_pass, results = replaygate.run_exit_check(argv or None)
    spent = max(0.0, usage.today_spend() - before)

    print(f"\n{'=' * 60}")
    for r in results:
        status = ("SKIP" if r.get("skipped")
                  else "PASS" if replaygate._ok(r) else "FAIL")
        print(f"  {status}  run {r['run_id']}  — {r['detail']}")
    passed = sum(1 for r in results if replaygate._ok(r))
    print(f"Reliability gate: {passed}/{len(results)} runs replayed "
          "byte-identically (zero new API calls on replay).")
    print(f"Spend this run: ${spent:.4f}  (cap ${cap:.2f}).")

    if all_pass and (cap <= 0 or spent <= cap):
        print("✓ RELIABILITY GATE MET — Olympus runs unattended and "
              "reproducibly.")
        return 0
    if not replaygate.genuine_failures(results):
        print("⚠ INCONCLUSIVE — the gate couldn't run (provider unavailable, "
              "e.g. out of credits). Not a reliability failure; fix the account "
              "and re-run.")
        return 0
    print("✗ RELIABILITY GATE NOT MET — see the failing run(s) above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
