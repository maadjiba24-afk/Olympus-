"""Native quorum consensus — tally primitives + safety-biased verdict fold."""

import threading

from olympus import backend, config, consensus, evolve, orchestrator, store

_PASS = ('ok\nVERDICT: {"status": "pass", "unsupported_claims": [], '
         '"confidence": 0.9}')


# --- gating + counts -----------------------------------------------------

def test_disabled_by_default():
    assert consensus.enabled() is False


def test_enabled_and_verifier_count(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONSENSUS", "on")
    assert consensus.enabled() is True
    assert consensus.verifier_count() == 3                 # default, odd
    monkeypatch.setenv("OLYMPUS_CONSENSUS_VERIFIERS", "4")
    assert consensus.verifier_count() == 5                 # even bumped to odd
    monkeypatch.setenv("OLYMPUS_CONSENSUS_VERIFIERS", "99")
    assert consensus.verifier_count() == consensus._MAX_VERIFIERS


def test_thresholds():
    assert consensus.majority_threshold(3) == 2
    assert consensus.majority_threshold(5) == 3
    # Byzantine ⌊2n/3⌋+1 == 2f+1 for n = 3f+1.
    assert consensus.byzantine_threshold(4) == 3           # f=1
    assert consensus.byzantine_threshold(7) == 5           # f=2
    assert consensus.byzantine_threshold(3) == 3


# --- decide --------------------------------------------------------------

def test_decide_majority_met():
    r = consensus.decide(["a", "a", "b"])
    assert r.winner == "a" and r.count == 2 and r.met is True


def test_decide_no_quorum():
    r = consensus.decide(["a", "b", "c"])
    assert r.met is False                                  # 1 < threshold 2


def test_decide_deterministic_tiebreak():
    # a and b tie at 1 each in a 2-vote set; str-asc tie-break picks "a".
    r = consensus.decide(["b", "a"])
    assert r.winner == "a"


def test_decide_byzantine_stricter():
    votes = ["ok", "ok", "ok", "bad"]                     # 3/4
    assert consensus.decide(votes).met is True            # majority (>=3)
    assert consensus.decide(votes, byzantine=True).met is True   # >=3
    votes2 = ["ok", "ok", "bad", "bad"]                   # 2/4
    assert consensus.decide(votes2, byzantine=True).met is False


def test_decide_empty():
    r = consensus.decide([])
    assert r.winner is None and r.met is False


def test_decide_weighted():
    r = consensus.decide_weighted([("x", 0.6), ("y", 0.3), ("x", 0.1)])
    assert r.winner == "x" and r.met is True              # 0.7 > 0.5 total


# --- safest_verdict (safety-biased fold) ---------------------------------

def _v(status, claims=(), conf=0.8):
    return {"status": status, "unsupported_claims": list(claims),
            "confidence": conf}


def test_verdict_quorum_reject():
    out = consensus.safest_verdict([_v("reject"), _v("reject"), _v("pass")])
    assert out["status"] == "reject"


def test_verdict_unanimous_pass():
    out = consensus.safest_verdict([_v("pass"), _v("pass"), _v("pass")])
    assert out["status"] == "pass"


def test_verdict_dissent_lands_on_warn():
    # pass reaches majority but a lone reject exists → NOT pass; cautious warn.
    out = consensus.safest_verdict([_v("pass"), _v("pass"), _v("reject")])
    assert out["status"] == "warn"


def test_verdict_no_majority_is_warn():
    out = consensus.safest_verdict([_v("pass"), _v("warn"), _v("reject")])
    assert out["status"] == "warn"


def test_verdict_unions_claims_from_non_pass():
    out = consensus.safest_verdict([
        _v("reject", ["claim A"]),
        _v("warn", ["claim B", "claim A"]),
        _v("pass", ["ignored"]),
    ])
    assert out["unsupported_claims"] == ["claim A", "claim B"]   # deduped union
    assert "ignored" not in out["unsupported_claims"]            # pass excluded


def test_verdict_confidence_mean_and_agreement():
    out = consensus.safest_verdict([_v("pass", conf=0.9), _v("pass", conf=0.7),
                                    _v("pass", conf=0.8)])
    assert out["confidence"] == 0.8
    assert out["agreement"] == 1.0
    assert out["votes"] == {"pass": 3}


def test_verdict_empty_is_cautious():
    out = consensus.safest_verdict([])
    assert out["status"] == "warn" and out["confidence"] == 0.0


def test_lens_cycles():
    assert consensus.lens_for(0) == consensus.LENSES[0]
    assert consensus.lens_for(len(consensus.LENSES)) == consensus.LENSES[0]


# --- hardening: a pass verdict never carries unsupported claims ----------

def test_pass_verdict_clears_claims():
    # n=5, passes=3 (quorum), zero rejects → status pass; the two warns' claims
    # must NOT ride along on a clean bill of health.
    out = consensus.safest_verdict([
        _v("pass"), _v("pass"), _v("pass"),
        _v("warn", ["lingering doubt"]), _v("warn", ["another"]),
    ])
    assert out["status"] == "pass"
    assert out["unsupported_claims"] == []       # cleared on pass
    # warn still keeps them
    warned = consensus.safest_verdict([_v("warn", ["x"]), _v("warn", ["y"]),
                                       _v("pass")])
    assert warned["status"] == "warn" and warned["unsupported_claims"] == ["x", "y"]


# --- hardening: orchestrator quorum floor under verifier failure ---------

def _consensus_bot(monkeypatch, verdict_count):
    """A bot whose N=3 verifier fan-out returns a parseable verdict for the
    first `verdict_count` calls and unparseable text for the rest."""
    monkeypatch.setenv("OLYMPUS_CONSENSUS", "1")
    monkeypatch.setenv("OLYMPUS_CONSENSUS_VERIFIERS", "3")
    bot = orchestrator.Olympus(user="qfloor")
    lock, calls = threading.Lock(), {"n": 0}

    def fake_run_agent(settings, system, task, tool_defs,
                       mcp_servers=None, effort="high"):
        with lock:
            calls["n"] += 1
            i = calls["n"]
        return _PASS if i <= verdict_count else "no verdict in this reply"

    monkeypatch.setattr(backend, "run_agent", fake_run_agent)
    return bot


def test_quorum_floor_degrades_when_too_few_verdicts(monkeypatch):
    # Only 1 of 3 verifiers produced a verdict (< majority 2) → visible degraded
    # path (verdict None), NOT a lone verifier's pass deciding.
    bot = _consensus_bot(monkeypatch, verdict_count=1)
    vs = config.Settings(provider="anthropic", model="claude-opus-4-8")
    content, verdict = bot._verify_consensus("sys", "task", vs, [], [])
    assert verdict is None


def test_quorum_met_returns_verdict(monkeypatch):
    # 2 of 3 verifiers agree pass → quorum met → an aggregated verdict returns.
    bot = _consensus_bot(monkeypatch, verdict_count=2)
    vs = config.Settings(provider="anthropic", model="claude-opus-4-8")
    content, verdict = bot._verify_consensus("sys", "task", vs, [], [])
    assert verdict is not None and verdict["status"] == "pass"


# --- self-evolution: consensus tunes its own panel over time -------------

def test_verifier_count_reads_self_tuned_value(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CONSENSUS_VERIFIERS", raising=False)
    store.reset()
    assert consensus.verifier_count() == 3            # default
    evolve._set_param("consensus", "verifiers", 5)
    assert consensus.verifier_count() == 5            # picks up the tuned value
    store.reset()


def test_env_override_beats_self_tuned(monkeypatch):
    store.reset()
    evolve._set_param("consensus", "verifiers", 7)
    monkeypatch.setenv("OLYMPUS_CONSENSUS_VERIFIERS", "1")
    assert consensus.verifier_count() == 1            # operator override wins
    store.reset()


def test_tuned_value_is_coerced_odd_and_clamped(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CONSENSUS_VERIFIERS", raising=False)
    store.reset()
    evolve._set_param("consensus", "verifiers", 4)    # even
    assert consensus.verifier_count() == 5            # bumped to odd
    store.reset()


def test_evolution_widens_panel_on_sustained_degradation(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CONSENSUS_VERIFIERS", raising=False)
    store.reset()
    before = consensus.verifier_count()               # 3
    # Simulate the quorum failing to form, run after run.
    for _ in range(evolve._MIN_SAMPLES + 2):
        evolve.record("consensus", evolve.DEGRADED, "1/3 verdicts")
    evolve.review()                                   # the periodic tuner
    after = consensus.verifier_count()
    assert after > before                             # panel widened itself
    assert after <= consensus._MAX_VERIFIERS
    store.reset()
