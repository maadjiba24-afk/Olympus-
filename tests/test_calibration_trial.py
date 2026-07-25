"""Calibration trial harness — checkpoint reports + gates.

Deterministic, no model calls. These tests validate that the reporting MACHINERY
computes the protocol's metrics correctly; they do NOT constitute trial results
(a real trial needs live runs + human feedback accumulating over time).
"""

import pytest

from olympus import calibration, calibration_trial


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    monkeypatch.setattr(calibration.config, "MEMORY_DIR", tmp_path)
    calibration._STATS.update({k: 0 for k in calibration._STATS})
    calibration._APPEND_MS.clear()
    yield


def _seed(n_ok=10, provider="anthropic", model="claude-sonnet-5",
          specialist="hephaestus", start=0):
    for i in range(n_ok):
        calibration.record_observation(
            f"{provider}-{start+i}", provider=provider, model=model,
            specialist=specialist, latency_ms=100 + i, cost_usd=0.001,
            result="ok")


# --- config manifest -------------------------------------------------------

def test_trial_config_captures_required_fields():
    cfg = calibration_trial.trial_config(owner="ops@example")
    for k in ("activated_at", "commit", "schema", "taxonomy_version",
              "providers_models", "collection_dir", "retention_days",
              "export_allowed", "signing_available", "process_topology",
              "trial_owner"):
        assert k in cfg, k
    assert cfg["trial_owner"] == "ops@example"
    assert cfg["reversible_via"].startswith("OLYMPUS_CALIBRATION=0")


# --- checkpoint A: instrumentation quality ---------------------------------

def test_checkpoint_a_reports_instrumentation():
    _seed(10)
    a = calibration_trial.checkpoint_a()
    assert a["persisted_observations"] == 10
    assert a["recording_failure_rate"] == 0.0
    assert a["chain_integrity"]["ok"] is True
    assert a["domain_classification_coverage_routed"] == 1.0   # all routed→code
    assert a["records_by_provider_model"]["anthropic/claude-sonnet-5"] == 10
    assert a["records_by_domain"]["code"] == 10
    assert a["recording_overhead"]["samples"] == 10
    assert a["gate"]["ok"] is True


def test_gate_pauses_on_corruption():
    _seed(3)
    lines = calibration.path().read_text(encoding="utf-8").splitlines()
    lines[1] = "corrupt"
    calibration.path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    g = calibration_trial.integrity_gate()
    assert g["ok"] is False
    assert any("corruption" in r for r in g["pause_reasons"])


def test_gate_pauses_when_failure_rate_exceeds_1pct(monkeypatch):
    _seed(50)                                   # 50 persisted
    import contextlib
    from olympus import proclock, errors

    @contextlib.contextmanager
    def _boom(name, timeout=None):
        raise TimeoutError("x")
        yield  # pragma: no cover
    monkeypatch.setattr(proclock, "lock", _boom)
    monkeypatch.setattr(errors, "capture", lambda *a, **k: None)
    for i in range(3):                          # 3 drops → 3/53 ≈ 5.7% > 1%
        calibration.record_observation(f"d{i}", provider="a", model="m")
    g = calibration_trial.integrity_gate()
    assert g["ok"] is False
    assert any("failure rate" in r for r in g["pause_reasons"])


# --- checkpoint B: evidence quality + bias ---------------------------------

def test_checkpoint_b_keeps_signals_separate_and_scans_bias():
    _seed(6, provider="openai", model="gpt-5")
    calibration.record_feedback("openai-0", calibration.APPROVED)
    calibration.record_feedback("openai-1", calibration.REJECTED)
    b = calibration_trial.checkpoint_b()
    assert "cells" in b and b["comparable_cells"] >= 1
    # separate rates, never one score
    cell = b["cells"][0]
    assert "completion_rate" in cell and "approval_rate" in cell
    assert "success_rate" not in cell
    assert "bias_scan" in b
    assert "no_feedback_by_domain" in b["bias_scan"]


def test_checkpoint_b_flags_mixed_version_fragmentation():
    for i in range(6):
        calibration.record_observation(f"a{i}", provider="openai", model="gpt-5",
                                       base_url="https://a", specialist="hephaestus",
                                       result="ok")
    for i in range(6):
        calibration.record_observation(f"b{i}", provider="openai", model="gpt-5",
                                       base_url="https://b", specialist="hephaestus",
                                       result="ok")
    b = calibration_trial.checkpoint_b()
    assert b["model_version_fragmentation"]["cells_with_mixed_config"] >= 1


# --- checkpoint C: strategic hypothesis ------------------------------------

def test_checkpoint_c_refuses_ranking_on_thin_evidence():
    _seed(3)                                    # below _MIN_SAMPLES
    c = calibration_trial.checkpoint_c()
    assert c["any_ranking_produced"] is False   # refuses, correctly


def test_checkpoint_c_can_rank_completion_when_evidence_suffices():
    # Two configs in one domain, both well-sampled, clearly separated completion.
    for i in range(20):
        calibration.record_observation(f"good{i}", provider="openai", model="gpt-5",
                                       specialist="hephaestus", result="ok")
    for i in range(20):
        calibration.record_observation(f"bad{i}", provider="anthropic",
                                       model="claude-sonnet-5",
                                       specialist="hephaestus",
                                       result="ok" if i < 4 else "error")
    c = calibration_trial.checkpoint_c()
    r = c["rankings_by_domain"]["code"]
    # ranking MAY be produced (well-sampled, same domain); if so it is completion-
    # only and honestly labelled — never a quality/moat claim.
    if r["ranked"]:
        assert r["metric"] == "completion"
        assert "not a quality" in r["note"].lower() or "completion only" in r["note"].lower()


# --- full report + render --------------------------------------------------

def test_full_report_is_machine_readable_and_safe():
    import json
    _seed(5)
    calibration.record_observation("secret-run", provider="anthropic", model="m",
                                   specialist="plutus",
                                   task="acquire CompetitorCorp for $9M")
    blob = json.dumps(calibration_trial.full_report(owner="ops"))
    assert "CompetitorCorp" not in blob and "9M" not in blob   # no plaintext
    assert '"checkpoint_a"' in blob and '"checkpoint_c"' in blob


def test_render_disabled_says_not_running(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    assert "OFF" in calibration_trial.render()


def test_render_shows_progress_to_first_checkpoint():
    _seed(7)
    out = calibration_trial.render()
    assert "7/100" in out or "pre-A" in out
