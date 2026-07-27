"""Tests for Capability C6 — the provider-drift regression tripwire.

Fixture-driven and KEYLESS: `run_fn` is injected so no model calls ever occur.
Covers the severity ladder, the hard budget cap (I-M1), keyed evidence rows
(I-M2), reversible freeze markers (I-M3), the default-off cadence (I-M4),
reproduce-before-believe, and clean-skip on an unreachable provider.
"""

from __future__ import annotations

import json

import pytest

from olympus import config, doctor, modelgate


def _settings() -> config.Settings:
    return config.Settings.from_env()


def _member() -> str:
    s = _settings()
    return f"{s.provider}/{s.model}"


def make_run_fn(domain_scores=None, *, cost=0.0, default=8.0,
                error_domains=(), all_error=False, unavailable=False):
    """A controlled scorer: returns fixed per-domain scores and costs."""
    domain_scores = domain_scores or {}
    error_domains = set(error_domains)

    def run_fn(settings, item):
        if unavailable:
            raise modelgate.ProviderUnavailable("no route to host")
        dom = item.get("domain") or item.get("specialist")
        if all_error or dom in error_domains:
            return {"score": 0.0, "cost": cost, "error": True}
        return {"score": domain_scores.get(dom, default), "cost": cost,
                "error": False}

    return run_fn


def _seed_baseline(scores_default=8.0):
    """Establish a keyed baseline (first run), then return the fresh settings."""
    s = _settings()
    res = modelgate.run_gate(s, run_fn=make_run_fn(default=scores_default))
    assert res.severity == modelgate.NO_BASELINE
    return s


# --- fingerprints ----------------------------------------------------------

def test_prompt_manifest_deterministic_and_changes(monkeypatch, tmp_path):
    m1 = modelgate.prompt_manifest()
    assert m1 == modelgate.prompt_manifest()          # deterministic
    assert "tmpl_ver" in m1 and len(m1["tmpl_ver"]) == 12
    # Every entry is a 12-char sha prefix.
    assert all(len(v) == 12 for v in m1.values())

    d = tmp_path / "prompts"
    d.mkdir()
    (d / "a.md").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(modelgate, "_prompts_dir", lambda: d)
    before = modelgate.prompt_manifest()
    (d / "a.md").write_text("hello world", encoding="utf-8")
    after = modelgate.prompt_manifest()
    assert before["a"] != after["a"]                  # per-file hash changed
    assert before["tmpl_ver"] != after["tmpl_ver"]    # combined changed


def test_flags_fingerprint_deterministic_and_changes(monkeypatch):
    f1 = modelgate.flags_fingerprint()
    assert f1 == modelgate.flags_fingerprint()
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    assert modelgate.flags_fingerprint() != f1


def test_eval_set_ver_is_stable_prefix():
    v = modelgate.eval_set_ver()
    assert len(v) == 12 and v == modelgate.eval_set_ver()


# --- baseline seeding ------------------------------------------------------

def test_first_run_seeds_baseline_no_gating():
    s = _settings()
    res = modelgate.run_gate(s, run_fn=make_run_fn(default=8))
    assert res.severity == modelgate.NO_BASELINE
    assert res.ok
    assert modelgate.load_baseline(modelgate.result_key(s))   # written
    assert not modelgate.frozen_members()


def test_within_tolerance_is_info():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=make_run_fn(default=8))
    assert res.severity == modelgate.INFO
    assert res.ok
    assert not modelgate.frozen_members()


# --- severity ladder -------------------------------------------------------

def test_classify_single_domain_is_warn_pending():
    baseline = {"code": 8.0, "routing": 8.0}
    result = {"scores": {"code": 3.0, "routing": 8.0}, "error_rate": 0.0}
    assert modelgate.classify(result, baseline) == modelgate.WARN_PENDING


def test_single_domain_stays_warn_pending_without_confirm():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=make_run_fn({"code": 3}, default=8),
                             confirm=False)
    assert res.severity == modelgate.WARN_PENDING
    assert res.ok
    assert not modelgate.frozen_members()


def test_single_domain_regression_confirms_to_warn():
    s = _seed_baseline(8.0)
    # The drop reproduces on the confirmation pass -> hardens to warn.
    res = modelgate.run_gate(s, run_fn=make_run_fn({"code": 3}, default=8),
                             confirm=True)
    assert res.severity == modelgate.WARN
    assert res.ok                                    # warn does not freeze
    assert not modelgate.frozen_members()


def test_multi_domain_regression_freezes_and_warns_doctor():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(
        s, run_fn=make_run_fn({"code": 3, "multilingual": 2}, default=8))
    assert res.severity == modelgate.FREEZE
    assert not res.ok
    frozen = modelgate.frozen_members()
    assert _member() in frozen
    assert frozen[_member()]["severity"] == modelgate.FREEZE
    checks = doctor._drift_checks()
    assert any(c.name == "provider drift" and c.status == doctor.WARN
               for c in checks)


def test_verify_domain_regression_blocks_and_fails_doctor():
    s = _seed_baseline(8.0)
    res = modelgate.run_gate(s, run_fn=make_run_fn({"verify": 2}, default=8))
    assert res.severity == modelgate.BLOCK
    assert not res.ok
    assert modelgate.frozen_members()[_member()]["severity"] == modelgate.BLOCK
    checks = doctor._drift_checks()
    assert any(c.name == "provider drift" and c.status == doctor.FAIL
               for c in checks)


def test_provider_errors_quarantine():
    s = _settings()
    res = modelgate.run_gate(s, run_fn=make_run_fn(all_error=True))
    assert res.severity == modelgate.QUARANTINE
    assert not res.ok
    assert modelgate.frozen_members()[_member()]["severity"] \
        == modelgate.QUARANTINE


# --- budget cap (I-M1) -----------------------------------------------------

def test_budget_cap_stops_mid_corpus_without_overrun():
    s = _settings()
    # The never-exceed guard is a PRE-FLIGHT estimate: inject a foresight
    # estimator matching the actual per-item cost so the stop point is exact.
    res = modelgate.run_gate(s, budget_usd=1.0,
                             run_fn=make_run_fn(default=8, cost=0.4),
                             cost_estimator=lambda _s, _i: 0.4)
    assert res.stopped_reason == modelgate.BUDGET_EXHAUSTED
    assert res.severity == modelgate.BUDGET_EXHAUSTED
    assert len(res.results) == 2                      # stops before item 3
    assert res.cost <= 1.0
    assert res.cost == pytest.approx(0.8)
    # A partial run neither seeds a baseline nor freezes anything.
    assert not modelgate.frozen_members()


# --- reversible markers (I-M3) ---------------------------------------------

def test_freeze_marker_delete_unfreezes():
    s = _seed_baseline(8.0)
    modelgate.run_gate(
        s, run_fn=make_run_fn({"code": 3, "multilingual": 2}, default=8))
    assert _member() in modelgate.frozen_members()
    assert modelgate.unfreeze(_member()) is True
    assert _member() not in modelgate.frozen_members()
    assert doctor._drift_checks()[0].status == doctor.OK


# --- keyed evidence rows (I-M2) --------------------------------------------

def test_result_row_carries_full_key():
    s = _seed_baseline(8.0)
    path = config.MEMORY_DIR / "modelgate" / "results.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    assert rows
    row = rows[-1]
    for field in ("provider", "model", "model_revision", "tmpl_ver",
                  "olympus_commit", "flags_fp", "eval_set_ver", "ts",
                  "scores", "cost"):
        assert field in row, f"missing key field: {field}"


# --- clean-skip on unreachable provider (exit-0, no false freeze) ----------

def test_provider_unavailable_is_clean_skip():
    s = _settings()
    res = modelgate.run_gate(s, run_fn=make_run_fn(unavailable=True))
    assert res.severity == modelgate.SKIPPED
    assert res.ok
    assert res.results == []
    assert not modelgate.frozen_members()             # not a false freeze


# --- heartbeat cadence (I-M4) ----------------------------------------------

def test_heartbeat_drift_gate_off_by_default(monkeypatch):
    from olympus import heartbeat
    monkeypatch.setattr(config, "DRIFT_GATE_EVERY", 0)
    # A non-positive cadence means OFF, not "every tick".
    assert heartbeat._due({}, "drift_gate", config.DRIFT_GATE_EVERY,
                          10_000_000.0) is False


def test_heartbeat_drift_gate_fires_and_records_state(monkeypatch):
    from olympus import firstrun, heartbeat
    monkeypatch.setattr(firstrun, "configured", lambda: False)
    monkeypatch.setattr(config, "DRIFT_GATE_EVERY", 3600)
    # Force ONLY the drift gate due, so the test is deterministic.
    monkeypatch.setattr(heartbeat, "_due",
                        lambda state, key, interval, now: key == "drift_gate")

    calls = {"n": 0}

    def fake_gate(settings, **kw):
        calls["n"] += 1
        return modelgate.GateResult(ok=True, severity="info", results=[],
                                    cost=0.0)

    monkeypatch.setattr(modelgate, "run_gate", fake_gate)

    state: dict = {}
    log = heartbeat.tick(state, now=10_000_000.0)
    assert calls["n"] == 1
    assert state.get("drift_gate") == 10_000_000.0
    assert any("Drift gate" in line for line in log)
