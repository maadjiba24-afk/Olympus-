"""Feature self-evolution: telemetry, health, and safe bounded auto-tuning.

The doctrine under test: measure everything; auto-tune ONLY registered,
non-security parameters within hard [lo, hi] guardrails; surface (not
impose) anything else. A runaway loop must be structurally impossible."""

import pytest

from olympus import config, curator, evolve, goals, moa


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


# --- telemetry + health -------------------------------------------------------

def test_record_and_health():
    for _ in range(7):
        evolve.record("moa", evolve.OK)
    evolve.record("moa", evolve.FAIL, "boom")
    h = evolve.health("moa")["moa"]
    assert h["samples"] == 8
    assert h["ok_rate"] == round(7 / 8, 3)
    assert h["last_failure"] == "boom"


def test_invalid_outcome_ignored():
    evolve.record("x", "nonsense")
    assert evolve.health("x")["x"] == {"samples": 0}


def test_telemetry_is_bounded():
    for i in range(evolve._MAX_PER_FEATURE + 50):
        evolve.record("f", evolve.OK)
    assert evolve.health("f")["f"]["samples"] == evolve._MAX_PER_FEATURE


# --- auto-tuning within guardrails --------------------------------------------

def test_degraded_feature_is_tuned_down_within_bounds():
    for _ in range(10):
        evolve.record("moa", evolve.DEGRADED)
    assert evolve.current("moa", "reference_count") == 4.0     # default
    evolve.review()
    tuned = evolve.current("moa", "reference_count")
    assert 2.0 <= tuned < 4.0                                  # down, clamped


def test_tuning_never_exceeds_guardrails():
    # Drive many reviews; the value must never leave [lo, hi] no matter what.
    for _ in range(50):
        evolve.record("moa", evolve.DEGRADED)
        evolve.review()
    v = evolve.current("moa", "reference_count")
    assert 2.0 <= v <= 6.0


def test_healthy_feature_is_not_tuned():
    for _ in range(20):
        evolve.record("goals", evolve.OK)
    before = evolve.current("goals", "check_backoff")
    evolve.review()
    assert evolve.current("goals", "check_backoff") == before


def test_backoff_increases_for_failing_goals():
    for _ in range(12):
        evolve.record("goals", evolve.FAIL, "stalled")
    evolve.review()
    assert evolve.current("goals", "check_backoff") > 1.0      # widened


def test_unknown_tunable_raises():
    with pytest.raises(KeyError):
        evolve.current("moa", "not_a_param")


def test_no_security_tunable_is_registered():
    # The registry must never expose an egress/auth/capability setting.
    banned = ("egress", "sovereign", "allowlist", "token", "key", "auth",
              "exec", "sandbox", "plugins")
    for key in evolve._TUNABLES:
        assert not any(b in key.lower() for b in banned), key


# --- review report + summary --------------------------------------------------

def test_review_reports_and_persists():
    for _ in range(10):
        evolve.record("learn", evolve.FAIL, "model down")
    text = evolve.review()
    assert "learn" in text and "DEGRADED" in text
    from olympus import memory
    assert "feature evolution" in memory.recent("reports", 5)


def test_review_ignores_small_samples():
    evolve.record("tiny", evolve.FAIL)
    text = evolve.review()
    assert "tiny" not in text                 # below _MIN_SAMPLES → not judged


def test_track_wrapper_records_ok_and_fail():
    assert evolve.track("w", lambda: 42) == 42
    assert evolve.health("w")["w"]["ok_rate"] == 1.0
    with pytest.raises(RuntimeError):
        evolve.track("w", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert evolve.health("w")["w"]["fail_rate"] > 0


def test_summary_shape():
    s = evolve.summary()
    assert "features" in s and "tunables" in s
    assert "moa.reference_count" in s["tunables"]
    t = s["tunables"]["moa.reference_count"]
    assert t["lo"] == 2 and t["hi"] == 6


# --- integration: features actually read their tuned values -------------------

def test_moa_reads_tuned_reference_count(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k")
    import json
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps(
        [{"provider": "openai", "model": f"m{i}", "api_key": "k"}
         for i in range(6)]))
    # Force the tuned value to its floor and confirm _members honours it.
    evolve._set_param("moa", "reference_count", 2)
    assert len(moa._members()) == 2
