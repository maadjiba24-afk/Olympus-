"""Feature self-evolution: telemetry, health, and safe bounded auto-tuning.

The doctrine under test: measure everything; auto-tune ONLY registered,
non-security parameters within hard [lo, hi] guardrails; surface (not
impose) anything else. A runaway loop must be structurally impossible."""

import pytest

from olympus import config, curator, evolve, goals, moa, store


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


@pytest.mark.parametrize(
    "blob",
    [
        b"{not-json",
        b"[]",
        b'{"operator":{"params":[]}}',
        b'{"operator":{"params":{"daily_ceiling":"25"}}}',
        b'{"operator":{"params":{"daily_ceiling":NaN}}}',
    ],
)
def test_malformed_tunable_evidence_is_rejected(blob):
    store.backend().put(evolve._NS, evolve._KEY, blob)

    with pytest.raises(evolve.EvolutionStateError):
        evolve.current("operator", "daily_ceiling")


def test_best_effort_telemetry_does_not_overwrite_corrupt_policy_evidence():
    corrupt = b"{not-json"
    store.backend().put(evolve._NS, evolve._KEY, corrupt)

    # A later telemetry event must not turn an unreadable, previously tightened
    # security policy into a new valid blob containing the loose defaults.
    evolve.record("operator", evolve.OK)

    assert store.backend().get(evolve._NS, evolve._KEY) == corrupt


def test_operator_repair_quarantines_corruption_before_restoring_defaults():
    corrupt = b'{"operator":{"params":{"daily_ceiling":"broken"}}}'
    store.backend().put(evolve._NS, evolve._KEY, corrupt)

    out = evolve.repair()

    assert "quarantined" in out.lower()
    assert store.backend().get(evolve._NS, evolve._QUARANTINE_KEY) == corrupt
    assert store.backend().get(evolve._NS, evolve._KEY) == b"{}"
    assert evolve.current("operator", "daily_ceiling") == 25


def test_operator_repair_never_rewrites_valid_state():
    evolve._set_param("operator", "daily_ceiling", 20)
    before = store.backend().get(evolve._NS, evolve._KEY)

    assert "no repair" in evolve.repair().lower()
    assert store.backend().get(evolve._NS, evolve._KEY) == before
    assert store.backend().get(evolve._NS, evolve._QUARANTINE_KEY) is None


def test_status_surfaces_corrupt_evidence_and_the_explicit_recovery():
    store.backend().put(evolve._NS, evolve._KEY, b"{not-json")

    status = evolve.summary()

    assert status["state"]["ok"] is False
    assert "evolve repair" in status["state"]["recovery"]
    assert status["tunables"] == {}


def test_status_surfaces_an_unreadable_backend(monkeypatch):
    monkeypatch.setattr(
        evolve,
        "_load",
        lambda: (_ for _ in ()).throw(OSError("store offline")),
    )

    status = evolve.summary()

    assert status["state"]["ok"] is False
    assert "unreadable" in status["state"]["reason"]


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


# --- tighten-only security knobs (earned-autonomy policy) ---------------------

def test_tighten_only_registration_rejects_a_loose_default():
    # default must sit at the LOOSE bound so the value can only move tighter.
    with pytest.raises(ValueError):
        evolve.register_tunable(evolve.Tunable(
            "x", "y", lo=5, hi=25, default=5, on_fail="decrease",
            tighten_only=True))          # loose bound is hi(25), not lo(5)
    with pytest.raises(ValueError):
        evolve.register_tunable(evolve.Tunable(
            "x", "z", lo=20, hi=100, default=100, on_fail="increase",
            tighten_only=True))          # loose bound is lo(20), not hi(100)


def test_operator_knobs_tighten_on_degradation_and_never_loosen():
    for _ in range(3):
        evolve.record("operator", evolve.OK)
    for _ in range(7):
        evolve.record("operator", evolve.FAIL, "checkpoint:captcha")
    # defaults before any review
    assert evolve.current("operator", "establish_after") == 20
    assert evolve.current("operator", "daily_ceiling") == 25
    evolve.review()
    est = evolve.current("operator", "establish_after")
    cool = evolve.current("operator", "cooldown_secs")
    ceil = evolve.current("operator", "daily_ceiling")
    assert est > 20 and cool > 3600 and ceil < 25          # tightened
    # now the operator recovers — a healthy feature is NEVER loosened back
    for _ in range(40):
        evolve.record("operator", evolve.OK)
    evolve.review()
    assert evolve.current("operator", "establish_after") == est
    assert evolve.current("operator", "cooldown_secs") == cool
    assert evolve.current("operator", "daily_ceiling") == ceil


def test_operator_knobs_stay_within_guardrails_under_sustained_failure():
    for _ in range(60):
        evolve.record("operator", evolve.FAIL)
        evolve.review()
    assert 20 <= evolve.current("operator", "establish_after") <= 100
    assert 3600 <= evolve.current("operator", "cooldown_secs") <= 86400
    assert 5 <= evolve.current("operator", "daily_ceiling") <= 25


def test_reset_widens_tightened_knobs_back_to_defaults():
    for _ in range(10):
        evolve.record("operator", evolve.FAIL)
    evolve.review()
    assert evolve.current("operator", "daily_ceiling") < 25
    out = evolve.reset("operator")
    assert "operator.daily_ceiling" in out
    assert evolve.current("operator", "establish_after") == 20
    assert evolve.current("operator", "cooldown_secs") == 3600
    assert evolve.current("operator", "daily_ceiling") == 25
    # telemetry is preserved (only tuned params are cleared)
    assert evolve.health("operator")["operator"]["samples"] == 10
