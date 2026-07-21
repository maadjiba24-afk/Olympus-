#!/usr/bin/env python3
"""Answer-quality regression gate (M5 / closes SECURITY_RESIDUALS §6).

The unit suite proves the guardrails are correct; it does NOT prove that a
given answer is *good*. This gate closes that gap in CI: it runs the benchmark
(`olympus eval`), compares per-specialist averages against the committed
baseline (`olympus/quality_baseline.json`), and exits nonzero when any
specialist regresses by more than the tolerance — so a prompt/skill change that
quietly degrades answer quality fails the build instead of shipping.

Design (mirrors scripts/tier1_exit_check.py + .github/workflows/replay-gate.yml):
- Needs a real ANTHROPIC_API_KEY (the eval makes real model calls). With NO key
  the gate SKIPS CLEANLY (exit 0) — CI without the secret is not a failure.
- The comparison itself is the PURE `evals.regression_check`, unit-tested
  without a key in tests/test_quality_gate.py.
- Establish/refresh the baseline with `--update-baseline` (a human act, run
  once with a key; the new baseline is committed).

Exit codes: 0 = pass or skipped; 1 = regression/missing coverage; 2 = eval error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running from a source checkout without an editable install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from olympus import config, evals  # noqa: E402


def _has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or config.Settings.from_env().api_key)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Answer-quality regression gate")
    ap.add_argument("--tolerance", type=float, default=evals.DEFAULT_TOLERANCE,
                    help="max allowed per-specialist drop vs baseline (of 10)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="write the fresh scores as the new committed baseline")
    args = ap.parse_args(argv)

    if not _has_key():
        print("No model API key set — skipping the answer-quality gate "
              "(set ANTHROPIC_API_KEY to enable).")
        return 0

    try:
        scores = evals.per_specialist_scores()
    except Exception as err:                    # a real eval/infra failure
        print(f"Answer-quality gate could not run the benchmark: {err}",
              file=sys.stderr)
        return 2

    settings = config.Settings.from_env()
    current_model = settings.model or os.environ.get("OLYMPUS_MODEL", "")

    if args.update_baseline:
        import time
        payload = {
            "_provenance": {
                "date": time.strftime("%Y-%m-%d"),
                "endpoint": settings.base_url or settings.provider,
                "model": current_model,
            },
            "scores": {k: round(float(v), 2) for k, v in scores.items()},
        }
        evals.BASELINE_PATH.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"Wrote baseline for {len(scores)} specialists "
              f"(model {current_model}) to {evals.BASELINE_PATH.name}:")
        for s in sorted(scores):
            print(f"  {s}: {scores[s]}/10")
        return 0

    baseline = evals.load_baseline()
    if not baseline:
        print("No committed quality baseline yet — run this gate once with a "
              "key and --update-baseline to establish it. Reporting scores "
              "without gating:")
        print(evals.format_gate_report(scores, {"ok": True}, args.tolerance))
        return 0

    # Scores are model-dependent: enforce only when the current eval model is
    # the one that produced the baseline. On a different model (operator
    # switched providers/keys), report instead of gating — a red build from an
    # apples-to-oranges comparison would be noise, and a green one a lie.
    base_model = evals.load_baseline_meta().get("model")
    if base_model and current_model and base_model != current_model:
        print(f"Baseline was scored by '{base_model}' but this run uses "
              f"'{current_model}' — reporting without gating. To re-enable "
              "gating on the new model, refresh the baseline with "
              "--update-baseline and commit it.")
        print(evals.format_gate_report(scores, {"ok": True}, args.tolerance))
        return 0

    result = evals.regression_check(scores, baseline, args.tolerance)
    print(evals.format_gate_report(scores, result, args.tolerance))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
