"""Answer-quality regression gate (M5 / closes SECURITY_RESIDUALS §6).

The gate has two halves:
- a PURE comparison (`evals.regression_check`) that decides pass/fail from a
  fresh score dict + a committed baseline — fully tested here without a key;
- a thin CI wrapper (`scripts/quality_gate.py`) that runs the live benchmark
  and skips cleanly when no model key is present — the skip path is tested here
  via a real subprocess with the key stripped.

The live benchmark run itself (`olympus eval` producing real per-specialist
scores against a real model) needs an operator ANTHROPIC_API_KEY and is out of
scope for the unit suite — that is acceptance step (b), run by a maintainer.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from olympus import evals


# --- pure regression comparison ------------------------------------------

def test_no_regression_passes():
    base = {"plutus": 8.0, "aegis": 7.0}
    fresh = {"plutus": 8.0, "aegis": 7.5}       # equal or better
    r = evals.regression_check(fresh, base, tolerance=1.0)
    assert r["ok"] and not r["regressions"] and not r["missing"]


def test_small_drop_within_tolerance_passes():
    base = {"plutus": 8.0}
    fresh = {"plutus": 7.1}                      # −0.9, within 1.0
    assert evals.regression_check(fresh, base, tolerance=1.0)["ok"]


def test_drop_beyond_tolerance_fails():
    base = {"plutus": 8.0, "aegis": 7.0}
    fresh = {"plutus": 6.5, "aegis": 7.0}       # plutus −1.5 > 1.0
    r = evals.regression_check(fresh, base, tolerance=1.0)
    assert not r["ok"]
    assert r["regressions"][0]["specialist"] == "plutus"
    assert r["regressions"][0]["drop"] == 1.5


def test_new_specialist_not_a_regression():
    # A specialist absent from the baseline is new coverage, never a failure.
    base = {"plutus": 8.0}
    fresh = {"plutus": 8.0, "iris": 3.0}
    assert evals.regression_check(fresh, base, tolerance=1.0)["ok"]


def test_missing_baseline_specialist_fails_closed():
    # A baseline specialist with no fresh score = a silently dropped benchmark;
    # it must FAIL the gate, not pass as green.
    base = {"plutus": 8.0, "aegis": 7.0}
    fresh = {"plutus": 8.0}                      # aegis not scored
    r = evals.regression_check(fresh, base, tolerance=1.0)
    assert not r["ok"] and r["missing"] == ["aegis"]


def test_worst_regression_ranked_first():
    base = {"a": 9.0, "b": 9.0}
    fresh = {"a": 7.5, "b": 5.0}                # a −1.5, b −4.0
    r = evals.regression_check(fresh, base, tolerance=1.0)
    assert [x["specialist"] for x in r["regressions"]] == ["b", "a"]


# --- baseline loading -----------------------------------------------------

def test_load_baseline_roundtrip(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"scores": {"plutus": 8.2, "aegis": 7.5}}))
    assert evals.load_baseline(p) == {"plutus": 8.2, "aegis": 7.5}


def test_load_missing_baseline_is_empty(tmp_path):
    assert evals.load_baseline(tmp_path / "nope.json") == {}


def test_format_gate_report_shows_verdict():
    base = {"plutus": 8.0}
    fresh = {"plutus": 6.0}
    r = evals.regression_check(fresh, base, tolerance=1.0)
    out = evals.format_gate_report(fresh, r, 1.0)
    assert "REGRESSION plutus" in out and out.strip().endswith("FAIL")


# --- CI wrapper: clean skip when no key (real subprocess) -----------------

def test_gate_script_skips_without_key():
    script = Path(__file__).resolve().parent.parent / "scripts" / "quality_gate.py"
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLYMPUS_API_KEY")}
    env["OLYMPUS_MODEL"] = "claude-opus-4-8"
    r = subprocess.run([sys.executable, str(script)],
                       capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "skipping the answer-quality gate" in r.stdout.lower()
