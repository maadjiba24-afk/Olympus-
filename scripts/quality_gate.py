#!/usr/bin/env python3
"""Explicitly authorized live quality comparison, separate from automatic mock CI.

Exit codes: 0 = measured pass / explicitly requested baseline update;
1 = regression or missing coverage; 2 = benchmark error;
3 = authorization, credential, or comparable baseline unavailable.
See docs/LIVE_QUALITY_AUTHORIZATION.md. No key-based automatic activation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import os
import sys

# Allow running from a source checkout without an editable install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from olympus import config, evals, live_quality_authorization as auth  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Answer-quality regression gate")
    ap.add_argument("--tolerance", type=float, default=evals.DEFAULT_TOLERANCE,
                    help="max allowed per-specialist drop vs baseline (of 10)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="write the fresh scores as the new committed baseline")
    auth.add_arguments(ap)
    args = ap.parse_args(argv)

    try:
        authorization = auth.authorize(args, Path(__file__).resolve().parent.parent)
        settings = config.Settings(**auth.provider_configuration(authorization))
        if not math.isfinite(args.tolerance) or not 0 <= args.tolerance <= 10:
            raise auth.AuthorizationRequired("tolerance must be finite and within 0-10")
    except (auth.AuthorizationRequired, OSError) as error:
        print(f"UNAVAILABLE: {error}; benchmark not run.", file=sys.stderr)
        return 3

    current_model = settings.model
    baseline = evals.load_baseline()
    meta = evals.load_baseline_meta()
    if not args.update_baseline and (
        not baseline or meta.get("model") != current_model
        or meta.get("endpoint") != (settings.base_url or settings.provider)
    ):
        print("UNAVAILABLE: no comparable baseline for the authorized model and endpoint; "
              "benchmark not run. Establish a baseline only through a separately "
              "reviewed --update-baseline invocation.", file=sys.stderr)
        return 3

    try:
        with evals.single_model_benchmark(settings):
            scores = evals.per_specialist_scores(settings=settings)
    except Exception as err:                    # a real eval/infra failure
        print(f"Answer-quality benchmark unavailable ({type(err).__name__})",
              file=sys.stderr)
        return 2

    if args.update_baseline:
        import time
        payload = {
            "_provenance": {
                "date": time.strftime("%Y-%m-%d"),
                "authorization": authorization,
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

    result = evals.regression_check(scores, baseline, args.tolerance)

    # Confirmation pass: single-run averages carry judge noise beyond the
    # tolerance, so a first-pass regression is re-scored in an independent
    # eval of ONLY the flagged specialists and fails only if it reproduces.
    # Skipped when baseline coverage is missing — that fails regardless.
    if result["regressions"] and not result["missing"]:
        flagged = [r["specialist"] for r in result["regressions"]]
        print(f"First pass flagged {len(flagged)} regression(s): "
              f"{', '.join(flagged)} — running an independent confirmation "
              "eval of just those specialists...")
        try:
            with evals.single_model_benchmark(settings):
                retry = evals.per_specialist_scores(settings=settings, only_specialists=flagged)
        except Exception as err:
            # No second opinion available — keep the first verdict (fail
            # closed), never pass on an unconfirmed hunch.
            print(f"Confirmation eval failed ({type(err).__name__}); keeping the first-pass "
                  "verdict.", file=sys.stderr)
        else:
            for s in sorted(retry):
                print(f"  confirm {s}: {retry[s]}/10 (first pass "
                      f"{scores.get(s)}/10, baseline {baseline.get(s)}/10)")
            result = evals.confirm_regressions(result, retry, baseline,
                                               args.tolerance)
            scores = {**scores, **retry}
            if result["ok"]:
                print("First-pass drops did not reproduce — judged as noise, "
                      "not regression.")

    print(evals.format_gate_report(scores, result, args.tolerance))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
