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


# --- baseline provenance (model-mismatch guard) ---------------------------

def test_load_baseline_meta_roundtrip(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({
        "_provenance": {"model": "moonshot-v1-32k", "date": "2026-07-21"},
        "scores": {"plutus": 9.6},
    }))
    assert evals.load_baseline_meta(p)["model"] == "moonshot-v1-32k"
    assert evals.load_baseline(p) == {"plutus": 9.6}   # scores unaffected


def test_load_baseline_meta_absent_is_empty(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"scores": {"plutus": 9.6}}))
    assert evals.load_baseline_meta(p) == {}
    assert evals.load_baseline_meta(tmp_path / "nope.json") == {}


def test_committed_baseline_records_its_model():
    # The shipped baseline must carry provenance, else the mismatch guard
    # cannot tell which model scored it and would gate apples vs oranges.
    meta = evals.load_baseline_meta()
    if evals.load_baseline():                  # only once a baseline exists
        assert meta.get("model"), "committed baseline lacks _provenance.model"


# --- CI provider resolver (pure selection logic) --------------------------

def _resolver():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "ci_provider_resolve.py"
    spec = importlib.util.spec_from_file_location("ci_provider_resolve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pick_model_prefers_listed_order():
    r = _resolver()
    ids = ["gpt-4o", "gpt-5", "gpt-4.1"]
    assert r.pick_model(ids, ["gpt-5.1", "gpt-5", "gpt-4o"], "gpt") == "gpt-5"


def test_pick_model_falls_back_to_prefix_then_first():
    r = _resolver()
    assert r.pick_model(["other", "kimi-k9"], ["nope"], "kimi") == "kimi-k9"
    assert r.pick_model(["only-model"], ["nope"], "zzz") == "only-model"
    assert r.pick_model([], ["nope"], "zzz") is None


def test_pick_model_kimi_account_inventory_stays_on_baseline_model():
    # Regression pin: the real Moonshot account inventory (no preview ids)
    # must keep resolving to moonshot-v1-32k — the model that produced the
    # committed baseline — or gating silently turns into report-only.
    r = _resolver()
    kimi_prefs = next(p for env, _, p, _ in r.OPENAI_COMPAT
                      if env == "KIMI_API_KEY")
    inventory = ["kimi-k2.7-code", "moonshot-v1-8k-vision-preview",
                 "moonshot-v1-128k-vision-preview", "kimi-k2.6",
                 "moonshot-v1-32k-vision-preview", "kimi-k2.5",
                 "moonshot-v1-128k", "kimi-k3", "moonshot-v1-auto",
                 "moonshot-v1-32k", "kimi-k2.7-code-highspeed",
                 "moonshot-v1-8k"]
    assert r.pick_model(inventory, kimi_prefs, "kimi") == "moonshot-v1-32k"


def test_resolver_with_no_keys_emits_nothing(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "ci_provider_resolve.py"
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and k != "GITHUB_ENV"}
    dest = tmp_path / "github.env"
    env["GITHUB_ENV"] = str(dest)
    r = subprocess.run([sys.executable, str(script)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    assert "skip" in r.stderr.lower()
    assert not dest.exists() or dest.read_text() == ""


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
